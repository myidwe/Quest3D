#define NOMINMAX
#define Q3D_DXGI_BUILD
#include "dxgi_snapshot_capture.h"

#include <algorithm>
#include <array>
#include <cstring>
#include <d3d11.h>
#include <dxgi1_6.h>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <unordered_map>
#include <windows.h>
#include <wrl/client.h>

namespace {
using Microsoft::WRL::ComPtr;
uint64_t qpc() {
  LARGE_INTEGER v{};
  QueryPerformanceCounter(&v);
  return v.QuadPart;
}
uint64_t freq() {
  LARGE_INTEGER v{};
  QueryPerformanceFrequency(&v);
  return v.QuadPart;
}
int32_t result(q3d_dxgi_error *error, int32_t status, uint32_t stage,
               HRESULT hr = S_OK, uint64_t capacity = 0) {
  if (error)
    *error = {1,        sizeof(*error), status, static_cast<int32_t>(hr),
              capacity, stage,          0};
  return status;
}
int32_t classify(HRESULT hr) {
  if (hr == DXGI_ERROR_WAIT_TIMEOUT)
    return Q3D_DXGI_TIMEOUT;
  if (hr == E_ACCESSDENIED)
    return Q3D_DXGI_ACCESS_DENIED;
  if (hr == DXGI_ERROR_ACCESS_LOST || hr == DXGI_ERROR_SESSION_DISCONNECTED)
    return Q3D_DXGI_SOURCE_CHANGED;
  if (hr == DXGI_ERROR_DEVICE_REMOVED || hr == DXGI_ERROR_DEVICE_RESET ||
      hr == DXGI_ERROR_DEVICE_HUNG)
    return Q3D_DXGI_DEVICE_LOST;
  if (hr == DXGI_ERROR_UNSUPPORTED || hr == E_NOINTERFACE)
    return Q3D_DXGI_UNSUPPORTED;
  return Q3D_DXGI_OS_ERROR;
}
uint64_t luid(LUID v) {
  return uint64_t(uint32_t(v.HighPart)) << 32 | v.LowPart;
}
bool physical_dpi() {
  return GetAwarenessFromDpiAwarenessContext(GetThreadDpiAwarenessContext()) ==
         DPI_AWARENESS_PER_MONITOR_AWARE;
}
bool fresh(uint64_t present, uint64_t now, uint64_t frequency,
           uint32_t max_age_ms) {
  return present && present <= now &&
         now - present <= frequency * max_age_ms / 1000;
}
bool source_matches(const q3d_dxgi_source &source, const DXGI_OUTPUT_DESC &desc,
                    const MONITORINFOEXW &info, LUID adapter_luid, bool dpi,
                    bool current) {
  if (!dpi || !current || !desc.AttachedToDesktop ||
      uint64_t(uintptr_t(desc.Monitor)) != source.hmonitor ||
      luid(adapter_luid) != source.adapter_luid ||
      desc.Rotation != source.rotation ||
      std::memcmp(desc.DeviceName, source.device_name,
                  sizeof(source.device_name)) ||
      std::wcscmp(info.szDevice, desc.DeviceName))
    return false;
  const RECT expected{source.left, source.top, source.left + LONG(source.width),
                      source.top + LONG(source.height)};
  return EqualRect(&desc.DesktopCoordinates, &expected) &&
         EqualRect(&info.rcMonitor, &expected);
}
struct session {
  DWORD thread = GetCurrentThreadId();
  HANDLE owner_thread = nullptr;
  uint64_t owner_birth = 0;
  bool closed = false;
  uint64_t frame_id = 0;
  std::mutex mutex;
  ComPtr<IDXGIFactory1> factory;
  ComPtr<IDXGIAdapter1> adapter;
  ComPtr<IDXGIOutput5> output;
  ComPtr<ID3D11Device> device;
  ComPtr<ID3D11DeviceContext> context;
  ComPtr<ID3D11Texture2D> staging;
  DXGI_FORMAT staging_format = DXGI_FORMAT_UNKNOWN;
  q3d_dxgi_source source{};
#ifdef Q3D_DXGI_TEST
  DWORD teardown_thread = 0;
#endif
  session() {
    if (!DuplicateHandle(GetCurrentProcess(), GetCurrentThread(),
                         GetCurrentProcess(), &owner_thread,
                         SYNCHRONIZE | THREAD_QUERY_LIMITED_INFORMATION, FALSE,
                         0))
      throw std::runtime_error("Cannot own opening thread lifetime");
    FILETIME birth{}, end{}, kernel{}, user{};
    if (!GetThreadTimes(owner_thread, &birth, &end, &kernel, &user)) {
      CloseHandle(owner_thread);
      owner_thread = nullptr;
      throw std::runtime_error("Cannot read opening thread identity");
    }
    owner_birth = uint64_t(birth.dwHighDateTime) << 32 | birth.dwLowDateTime;
  }
  bool owner_dead() const {
    return owner_thread &&
           WaitForSingleObject(owner_thread, 0) == WAIT_OBJECT_0;
  }
  bool is_owner() const {
    if (!owner_thread || thread != GetCurrentThreadId() ||
        WaitForSingleObject(owner_thread, 0) != WAIT_TIMEOUT)
      return false;
    FILETIME birth{}, end{}, kernel{}, user{};
    return GetThreadTimes(GetCurrentThread(), &birth, &end, &kernel, &user) &&
           (uint64_t(birth.dwHighDateTime) << 32 | birth.dwLowDateTime) ==
               owner_birth;
  }
  void teardown() {
#ifdef Q3D_DXGI_TEST
    if (owner_thread || context || device)
      teardown_thread = GetCurrentThreadId();
#endif
    if (context) {
      context->ClearState();
      context->Flush();
    }
    staging.Reset();
    context.Reset();
    device.Reset();
    output.Reset();
    adapter.Reset();
    factory.Reset();
    if (owner_thread) {
      CloseHandle(owner_thread);
      owner_thread = nullptr;
    }
  }
  ~session() { teardown(); }
  bool validate_source() const {
    if (!physical_dpi() || !factory->IsCurrent())
      return false;
    DXGI_OUTPUT_DESC desc{};
    MONITORINFOEXW info{};
    info.cbSize = sizeof(info);
    DXGI_ADAPTER_DESC1 ad{};
    const auto monitor = reinterpret_cast<HMONITOR>(uintptr_t(source.hmonitor));
    if (!GetMonitorInfoW(monitor, &info) || FAILED(output->GetDesc(&desc)) ||
        FAILED(adapter->GetDesc1(&ad)))
      return false;
    return source_matches(source, desc, info, ad.AdapterLuid, physical_dpi(),
                          factory->IsCurrent());
  }
};
std::mutex registry_mutex;
std::unordered_map<uint64_t, std::shared_ptr<session>> registry;
uint64_t next_handle =
    1; // Protected by registry_mutex; zero permanently exhausts the namespace.
std::shared_ptr<session> lookup(uint64_t handle) {
  std::lock_guard lock(registry_mutex);
  auto it = registry.find(handle);
  return it == registry.end() ? nullptr : it->second;
}
struct frame_guard {
  IDXGIOutputDuplication *dup;
  bool held = true;
  ~frame_guard() {
    if (held)
      dup->ReleaseFrame();
  }
};
struct map_guard {
  ID3D11DeviceContext *context;
  ID3D11Texture2D *texture;
  ~map_guard() { context->Unmap(texture, 0); }
};
int32_t snapshot_impl(session &s, void *pixels, uint64_t capacity,
                      uint32_t timeout_ms, uint32_t max_age_ms,
                      q3d_dxgi_frame *frame, q3d_dxgi_error *error) {
  if (!s.validate_source())
    return result(error, Q3D_DXGI_SOURCE_CHANGED, Q3D_STAGE_SOURCE);
  const std::array<DXGI_FORMAT, 4> formats{
      DXGI_FORMAT_R16G16B16A16_FLOAT, DXGI_FORMAT_B8G8R8A8_UNORM,
      DXGI_FORMAT_B8G8R8X8_UNORM, DXGI_FORMAT_R8G8B8A8_UNORM};
  ComPtr<IDXGIOutputDuplication> dup;
  auto hr = s.output->DuplicateOutput1(s.device.Get(), 0, formats.size(),
                                       formats.data(), dup.GetAddressOf());
  if (FAILED(hr))
    return result(error, classify(hr), Q3D_STAGE_DUPLICATE, hr);
  DXGI_OUTDUPL_FRAME_INFO info{};
  ComPtr<IDXGIResource> resource;
  const auto started = qpc();
  hr = dup->AcquireNextFrame(timeout_ms, &info, resource.GetAddressOf());
  const auto returned = qpc();
  if (FAILED(hr))
    return result(error, classify(hr), Q3D_STAGE_ACQUIRE, hr);
  frame_guard release{dup.Get()};
  const auto frequency = freq();
  const auto present = uint64_t(info.LastPresentTime.QuadPart);
  if (!fresh(present, returned, frequency, max_age_ms))
    return result(error, Q3D_DXGI_STALE_FRAME, Q3D_STAGE_ACQUIRE);
  ComPtr<ID3D11Texture2D> texture;
  hr = resource.As(&texture);
  if (FAILED(hr))
    return result(error, classify(hr), Q3D_STAGE_TEXTURE, hr);
  D3D11_TEXTURE2D_DESC td{};
  texture->GetDesc(&td);
  uint32_t bpp = 0;
  if (td.Format == DXGI_FORMAT_R16G16B16A16_FLOAT)
    bpp = 8;
  else if (td.Format == DXGI_FORMAT_B8G8R8A8_UNORM ||
           td.Format == DXGI_FORMAT_B8G8R8X8_UNORM ||
           td.Format == DXGI_FORMAT_R8G8B8A8_UNORM)
    bpp = 4;
  if (!bpp || td.SampleDesc.Count != 1 || td.ArraySize != 1 ||
      td.MipLevels != 1)
    return result(error, Q3D_DXGI_UNSUPPORTED, Q3D_STAGE_TEXTURE);
  if (td.Width != s.source.width || td.Height != s.source.height ||
      !s.validate_source())
    return result(error, Q3D_DXGI_SOURCE_CHANGED, Q3D_STAGE_SOURCE);
  const auto row_bytes = td.Width * bpp;
  const uint64_t required = uint64_t(row_bytes) * td.Height;
  if (capacity < required)
    return result(error, Q3D_DXGI_CAPACITY, Q3D_STAGE_CAPACITY, S_OK, required);
  if (!s.staging || s.staging_format != td.Format) {
    auto sd = td;
    sd.Usage = D3D11_USAGE_STAGING;
    sd.BindFlags = 0;
    sd.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    sd.MiscFlags = 0;
    s.staging.Reset();
    s.staging_format = DXGI_FORMAT_UNKNOWN;
    hr = s.device->CreateTexture2D(&sd, nullptr, s.staging.GetAddressOf());
    if (FAILED(hr))
      return result(error, classify(hr), Q3D_STAGE_TEXTURE, hr);
    s.staging_format = td.Format;
  }
  s.context->CopyResource(s.staging.Get(), texture.Get());
  D3D11_MAPPED_SUBRESOURCE mapped{};
  hr = s.context->Map(s.staging.Get(), 0, D3D11_MAP_READ, 0, &mapped);
  if (FAILED(hr))
    return result(error, classify(hr), Q3D_STAGE_MAP, hr);
  const auto mapped_at = qpc();
  uint64_t copied_at = 0;
  {
    map_guard unmap{s.context.Get(), s.staging.Get()};
    if (mapped.RowPitch < row_bytes || !mapped.pData)
      return result(error, Q3D_DXGI_OS_ERROR, Q3D_STAGE_MAP);
    if (!s.validate_source())
      return result(error, Q3D_DXGI_SOURCE_CHANGED, Q3D_STAGE_SOURCE);
    if (!fresh(present, qpc(), frequency, max_age_ms))
      return result(error, Q3D_DXGI_STALE_FRAME, Q3D_STAGE_MAP);
    auto dest = static_cast<unsigned char *>(pixels);
    auto src = static_cast<const unsigned char *>(mapped.pData);
    for (uint32_t y = 0; y < td.Height; ++y)
      std::memcpy(dest + uint64_t(y) * row_bytes,
                  src + uint64_t(y) * mapped.RowPitch, row_bytes);
    copied_at = qpc();
  }
  // A later failure cannot expose partially copied data as a valid new frame.
  auto fail_after_copy = [&](int32_t status, uint32_t stage,
                             HRESULT failure = S_OK) {
    std::memset(pixels, 0, static_cast<size_t>(required));
    return result(error, status, stage, failure);
  };
  texture.Reset();
  resource.Reset();
  hr = dup->ReleaseFrame();
  release.held = false;
  dup.Reset();
  if (FAILED(hr))
    return fail_after_copy(classify(hr), Q3D_STAGE_RELEASE, hr);
  hr = s.device->GetDeviceRemovedReason();
  if (FAILED(hr))
    return fail_after_copy(classify(hr), Q3D_STAGE_DEVICE, hr);
  if (!s.validate_source())
    return fail_after_copy(Q3D_DXGI_SOURCE_CHANGED, Q3D_STAGE_SOURCE);
  if (!fresh(present, qpc(), frequency, max_age_ms))
    return fail_after_copy(Q3D_DXGI_STALE_FRAME, Q3D_STAGE_COPY);
  q3d_dxgi_frame completed{};
  completed.version = 1;
  completed.size = sizeof(completed);
  completed.source = s.source;
  completed.format = td.Format;
  completed.row_bytes = row_bytes;
  completed.rows = td.Height;
  completed.bytes_per_pixel = bpp;
  completed.copied_bytes = required;
  completed.frame_id = ++s.frame_id;
  completed.last_present_qpc = present;
  completed.acquire_started_qpc = started;
  completed.acquire_returned_qpc = returned;
  completed.map_completed_qpc = mapped_at;
  completed.copy_completed_qpc = copied_at;
  completed.qpc_frequency = frequency;
  completed.last_mouse_qpc = info.LastMouseUpdateTime.QuadPart;
  completed.accumulated_frames = info.AccumulatedFrames;
  completed.protected_content_masked = info.ProtectedContentMaskedOut;
  *frame = completed;
  return result(error, Q3D_DXGI_OK, Q3D_STAGE_COPY, S_OK, required);
}
} // namespace

int32_t q3d_dxgi_open(uint64_t monitor, uint64_t *handle,
                      q3d_dxgi_source *source, uint32_t source_size,
                      q3d_dxgi_error *error) {
  if (handle)
    *handle = 0;
  if (source)
    std::memset(source, 0, std::min<size_t>(source_size, sizeof(*source)));
  if (!monitor || !handle || !source || source_size != sizeof(*source) ||
      !physical_dpi())
    return result(error, Q3D_DXGI_INVALID_ARGUMENT, Q3D_STAGE_ARGUMENT);
  try {
    auto s = std::make_shared<session>();
    auto hr = CreateDXGIFactory1(IID_PPV_ARGS(s->factory.GetAddressOf()));
    if (FAILED(hr))
      return result(error, classify(hr), Q3D_STAGE_SOURCE, hr);
    DXGI_OUTPUT_DESC found{};
    DXGI_ADAPTER_DESC1 found_adapter{};
    for (UINT ai = 0; !s->output; ++ai) {
      ComPtr<IDXGIAdapter1> adapter;
      hr = s->factory->EnumAdapters1(ai, adapter.GetAddressOf());
      if (hr == DXGI_ERROR_NOT_FOUND)
        break;
      if (FAILED(hr))
        return result(error, classify(hr), Q3D_STAGE_SOURCE, hr);
      for (UINT oi = 0; !s->output; ++oi) {
        ComPtr<IDXGIOutput> output;
        hr = adapter->EnumOutputs(oi, output.GetAddressOf());
        if (hr == DXGI_ERROR_NOT_FOUND)
          break;
        if (FAILED(hr))
          return result(error, classify(hr), Q3D_STAGE_SOURCE, hr);
        DXGI_OUTPUT_DESC desc{};
        hr = output->GetDesc(&desc);
        if (FAILED(hr))
          return result(error, classify(hr), Q3D_STAGE_SOURCE, hr);
        if (!desc.AttachedToDesktop ||
            uint64_t(uintptr_t(desc.Monitor)) != monitor)
          continue;
        hr = output.As(&s->output);
        if (FAILED(hr))
          return result(error, classify(hr), Q3D_STAGE_SOURCE, hr);
        s->adapter = adapter;
        found = desc;
        hr = adapter->GetDesc1(&found_adapter);
        if (FAILED(hr))
          return result(error, classify(hr), Q3D_STAGE_SOURCE, hr);
      }
    }
    if (!s->output)
      return result(error, Q3D_DXGI_SOURCE_CHANGED, Q3D_STAGE_SOURCE);
    auto width = found.DesktopCoordinates.right - found.DesktopCoordinates.left;
    auto height =
        found.DesktopCoordinates.bottom - found.DesktopCoordinates.top;
    if (width < 1 || height < 1 || width > 4096 || height > 2160 ||
        found.Rotation != DXGI_MODE_ROTATION_IDENTITY)
      return result(error, Q3D_DXGI_UNSUPPORTED, Q3D_STAGE_SOURCE);
    s->source = {1,
                 sizeof(q3d_dxgi_source),
                 monitor,
                 luid(found_adapter.AdapterLuid),
                 found.DesktopCoordinates.left,
                 found.DesktopCoordinates.top,
                 uint32_t(width),
                 uint32_t(height),
                 uint32_t(found.Rotation),
                 0,
                 {}};
    std::memcpy(s->source.device_name, found.DeviceName,
                sizeof(s->source.device_name));
    const D3D_FEATURE_LEVEL levels[]{D3D_FEATURE_LEVEL_11_1,
                                     D3D_FEATURE_LEVEL_11_0};
    hr = D3D11CreateDevice(s->adapter.Get(), D3D_DRIVER_TYPE_UNKNOWN, nullptr,
                           D3D11_CREATE_DEVICE_BGRA_SUPPORT, levels, 2,
                           D3D11_SDK_VERSION, s->device.GetAddressOf(), nullptr,
                           s->context.GetAddressOf());
    if (FAILED(hr))
      return result(error, classify(hr), Q3D_STAGE_DEVICE, hr);
    if (!s->validate_source())
      return result(error, Q3D_DXGI_SOURCE_CHANGED, Q3D_STAGE_SOURCE);
    uint64_t id = 0;
    {
      std::lock_guard lock(registry_mutex);
      if (!next_handle)
        return result(error, Q3D_DXGI_INTERNAL_ERROR, Q3D_STAGE_ARGUMENT);
      id = next_handle++;
      registry.emplace(id, s);
    }
    *source = s->source;
    *handle = id;
    return result(error, Q3D_DXGI_OK, Q3D_STAGE_DEVICE);
  } catch (...) {
    return result(error, Q3D_DXGI_INTERNAL_ERROR, Q3D_STAGE_DEVICE);
  }
}

int32_t q3d_dxgi_snapshot(uint64_t handle, void *pixels, uint64_t capacity,
                          uint32_t timeout_ms, uint32_t max_age_ms,
                          q3d_dxgi_frame *frame, uint32_t frame_size,
                          q3d_dxgi_error *error) {
  if (frame)
    std::memset(frame, 0, std::min<size_t>(frame_size, sizeof(*frame)));
  if (!pixels || !capacity || !frame || frame_size != sizeof(*frame) ||
      timeout_ms < 1 || timeout_ms > 1000 || max_age_ms < 1 || max_age_ms > 500)
    return result(error, Q3D_DXGI_INVALID_ARGUMENT, Q3D_STAGE_ARGUMENT);
  try {
    auto s = lookup(handle);
    if (!s)
      return result(error, Q3D_DXGI_INVALID_HANDLE, Q3D_STAGE_ARGUMENT);
    std::lock_guard lock(s->mutex);
    if (s->closed)
      return result(error, Q3D_DXGI_INVALID_HANDLE, Q3D_STAGE_ARGUMENT);
    if (s->owner_dead())
      return result(error, Q3D_DXGI_OWNER_DEAD, Q3D_STAGE_ARGUMENT);
    if (!s->is_owner())
      return result(error, Q3D_DXGI_WRONG_THREAD, Q3D_STAGE_ARGUMENT);
    return snapshot_impl(*s, pixels, capacity, timeout_ms, max_age_ms, frame,
                         error);
  } catch (...) {
    return result(error, Q3D_DXGI_INTERNAL_ERROR, Q3D_STAGE_COPY);
  }
}

int32_t q3d_dxgi_close(uint64_t handle, q3d_dxgi_error *error) {
  try {
    auto s = lookup(handle);
    if (!s)
      return result(error, Q3D_DXGI_INVALID_HANDLE, Q3D_STAGE_CLOSE);
    std::lock_guard lock(s->mutex);
    if (s->closed)
      return result(error, Q3D_DXGI_INVALID_HANDLE, Q3D_STAGE_CLOSE);
    if (!s->is_owner() && !s->owner_dead())
      return result(error, Q3D_DXGI_WRONG_THREAD, Q3D_STAGE_CLOSE);
    s->closed = true;
    // Complete COM teardown here, before a waiting foreign lookup can release
    // the final shared_ptr. After owner death this is a serialized cleanup-only
    // operation on a device that was not created with SINGLETHREADED.
    s->teardown();
    {
      std::lock_guard registry_lock(registry_mutex);
      registry.erase(handle);
    }
    return result(error, Q3D_DXGI_OK, Q3D_STAGE_CLOSE);
  } catch (...) {
    return result(error, Q3D_DXGI_INTERNAL_ERROR, Q3D_STAGE_CLOSE);
  }
}
