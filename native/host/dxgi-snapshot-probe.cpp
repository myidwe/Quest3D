// Standalone DXGI freshness/cleanup experiment. Never inject input, save screen
// images, change display settings, or share the operational Sunshine
// device/runtime.
#define NOMINMAX
#include <array>
#include <bit>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <d3d11.h>
#include <dwmapi.h>
#include <dxgi1_6.h>
#include <iomanip>
#include <iostream>
#include <nlohmann/json.hpp>
#include <psapi.h>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>
#include <windows.h>
#include <wrl/client.h>

using Microsoft::WRL::ComPtr;
using json = nlohmann::json;

std::uint64_t ticks() {
  LARGE_INTEGER t{};
  QueryPerformanceCounter(&t);
  return t.QuadPart;
}
std::uint64_t frequency() {
  static const auto value = [] {
    LARGE_INTEGER f{};
    QueryPerformanceFrequency(&f);
    return std::uint64_t(f.QuadPart);
  }();
  return value;
}
std::uint64_t ns(std::uint64_t t) {
  return t / frequency() * 1000000000ULL +
         (t % frequency()) * 1000000000ULL / frequency();
}
double milliseconds(std::uint64_t end, std::uint64_t begin) {
  return double(end - begin) * 1000.0 / frequency();
}
std::string hr_text(HRESULT hr) {
  std::ostringstream text;
  text << "0x" << std::hex << std::setw(8) << std::setfill('0')
       << static_cast<std::uint32_t>(hr);
  return text.str();
}
void checked(HRESULT hr, const char *operation) {
  if (FAILED(hr))
    throw std::runtime_error(std::string(operation) + " " + hr_text(hr));
}
void emit(const json &row) { std::cout << row.dump() << '\n' << std::flush; }

struct fixture_state {
  unsigned paints = 0;
};
LRESULT CALLBACK pattern_proc(HWND window, UINT message, WPARAM wparam,
                              LPARAM lparam) {
  auto state = reinterpret_cast<fixture_state *>(
      GetWindowLongPtrW(window, GWLP_USERDATA));
  if (message == WM_NCCREATE) {
    state = static_cast<fixture_state *>(
        reinterpret_cast<CREATESTRUCTW *>(lparam)->lpCreateParams);
    SetWindowLongPtrW(window, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(state));
  }
  if (message == WM_MOUSEACTIVATE)
    return MA_NOACTIVATE;
  if (message == WM_ERASEBKGND)
    return 1;
  if (message == WM_PAINT) {
    PAINTSTRUCT paint{};
    auto dc = BeginPaint(window, &paint);
    const std::array<COLORREF, 4> colors{RGB(255, 0, 0), RGB(0, 255, 0),
                                         RGB(0, 0, 255), RGB(255, 255, 255)};
    for (int i = 0; i < 4; ++i) {
      RECT rect{(i % 2) * 96, (i / 2) * 64, (i % 2 + 1) * 96, (i / 2 + 1) * 64};
      auto brush = CreateSolidBrush(colors[i]);
      FillRect(dc, &rect, brush);
      DeleteObject(brush);
    }
    EndPaint(window, &paint);
    if (state)
      ++state->paints;
    return 0;
  }
  return DefWindowProcW(window, message, wparam, lparam);
}
void pause_messages(DWORD duration) {
  auto until = GetTickCount64() + duration;
  do {
    MSG msg;
    while (PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE)) {
      TranslateMessage(&msg);
      DispatchMessageW(&msg);
    }
    if (GetTickCount64() < until)
      Sleep(1);
  } while (GetTickCount64() < until);
}

json memory(IDXGIAdapter3 *adapter) {
  DWORD handles = 0;
  GetProcessHandleCount(GetCurrentProcess(), &handles);
  PROCESS_MEMORY_COUNTERS_EX counters{};
  GetProcessMemoryInfo(GetCurrentProcess(),
                       reinterpret_cast<PROCESS_MEMORY_COUNTERS *>(&counters),
                       sizeof(counters));
  json out{
      {"process_handles", handles},
      {"gdi_objects", GetGuiResources(GetCurrentProcess(), GR_GDIOBJECTS)},
      {"user_objects", GetGuiResources(GetCurrentProcess(), GR_USEROBJECTS)},
      {"private_bytes", counters.PrivateUsage},
      {"working_set_bytes", counters.WorkingSetSize}};
  if (adapter) {
    DXGI_QUERY_VIDEO_MEMORY_INFO local{}, shared{};
    auto hr = adapter->QueryVideoMemoryInfo(0, DXGI_MEMORY_SEGMENT_GROUP_LOCAL,
                                            &local);
    out["local_query_hr"] = hr_text(hr);
    if (SUCCEEDED(hr)) {
      out["gpu_process_local_bytes"] = local.CurrentUsage;
      out["gpu_local_budget_bytes"] = local.Budget;
    }
    hr = adapter->QueryVideoMemoryInfo(0, DXGI_MEMORY_SEGMENT_GROUP_NON_LOCAL,
                                       &shared);
    if (SUCCEEDED(hr))
      out["gpu_process_nonlocal_bytes"] = shared.CurrentUsage;
  }
  return out;
}
float half(std::uint16_t v) {
  const int sign = (v & 0x8000) ? -1 : 1;
  const unsigned e = (v >> 10) & 31, m = v & 1023;
  if (e == 31)
    return m ? NAN : sign * INFINITY;
  return sign * std::ldexp(e ? 1.0f + float(m) / 1024.0f : float(m) / 1024.0f,
                           e ? int(e) - 15 : -14);
}
std::array<double, 3> pixel(const unsigned char *data, DXGI_FORMAT format) {
  if (format == DXGI_FORMAT_R16G16B16A16_FLOAT) {
    std::uint16_t v[4];
    std::memcpy(v, data, 8);
    return {half(v[0]), half(v[1]), half(v[2])};
  }
  if (format == DXGI_FORMAT_R8G8B8A8_UNORM)
    return {data[0] / 255.0, data[1] / 255.0, data[2] / 255.0};
  if (format == DXGI_FORMAT_B8G8R8A8_UNORM ||
      format == DXGI_FORMAT_B8G8R8X8_UNORM)
    return {data[2] / 255.0, data[1] / 255.0, data[0] / 255.0};
  throw std::runtime_error("unsupported acquired format");
}

struct experiment {
  ComPtr<IDXGIAdapter1> adapter;
  ComPtr<IDXGIAdapter3> adapter3;
  ComPtr<IDXGIOutput5> output;
  ComPtr<ID3D11Device> device;
  ComPtr<ID3D11DeviceContext> context;
  ComPtr<ID3D11Texture2D> staging;
  DXGI_OUTPUT_DESC desc{};
  HWND window = nullptr;
  DXGI_FORMAT staging_format = DXGI_FORMAT_UNKNOWN;
  std::uint64_t previous_roi_hash = 0;
  unsigned success = 0, timeout = 0, failed = 0, pixel_failures = 0;
  static constexpr std::array<DXGI_FORMAT, 4> formats{
      DXGI_FORMAT_R16G16B16A16_FLOAT, DXGI_FORMAT_B8G8R8A8_UNORM,
      DXGI_FORMAT_B8G8R8X8_UNORM, DXGI_FORMAT_R8G8B8A8_UNORM};

  HRESULT open(ComPtr<IDXGIOutputDuplication> &dup) {
    return output->DuplicateOutput1(device.Get(), 0, formats.size(),
                                    formats.data(), dup.GetAddressOf());
  }
  bool own_roi() const {
    if (!IsWindow(window) || !IsWindowVisible(window))
      return false;
    DWORD pid = 0;
    GetWindowThreadProcessId(window, &pid);
    if (pid != GetCurrentProcessId())
      return false;
    RECT rect{};
    if (!GetWindowRect(window, &rect) || rect.right - rect.left != 192 ||
        rect.bottom - rect.top != 128)
      return false;
    for (auto point :
         std::array<POINT, 4>{POINT{rect.left + 20, rect.top + 20},
                              POINT{rect.left + 172, rect.top + 20},
                              POINT{rect.left + 20, rect.top + 108},
                              POINT{rect.left + 172, rect.top + 108}})
      if (WindowFromPoint(point) != window)
        return false;
    return true;
  }
  json acquire(IDXGIOutputDuplication *dup, UINT timeout_ms, bool validate) {
    DXGI_OUTDUPL_FRAME_INFO info{};
    ComPtr<IDXGIResource> resource;
    auto started = ticks();
    auto hr = dup->AcquireNextFrame(timeout_ms, &info, resource.GetAddressOf());
    auto returned = ticks();
    json row{{"acquire_hr", hr_text(hr)},
             {"acquire_ms", milliseconds(returned, started)},
             {"acquire_started_qpc_ns", std::to_string(ns(started))},
             {"acquire_returned_qpc_ns", std::to_string(ns(returned))},
             {"timeout_ms", timeout_ms},
             {"source_timestamp_rewritten", false}};
    if (FAILED(hr)) {
      row["timeout"] = hr == DXGI_ERROR_WAIT_TIMEOUT;
      return row;
    }
    struct release_guard {
      IDXGIOutputDuplication *dup;
      bool held = true;
      ~release_guard() {
        if (held)
          dup->ReleaseFrame();
      }
    } release{dup};
    row["last_present_qpc_ticks"] =
        std::to_string(info.LastPresentTime.QuadPart);
    row["last_present_qpc_ns"] =
        std::to_string(ns(info.LastPresentTime.QuadPart));
    row["last_mouse_qpc_ticks"] =
        std::to_string(info.LastMouseUpdateTime.QuadPart);
    row["accumulated_frames"] = info.AccumulatedFrames;
    row["desktop_updated"] = info.LastPresentTime.QuadPart != 0;
    row["pointer_only"] = info.LastPresentTime.QuadPart == 0 &&
                          info.LastMouseUpdateTime.QuadPart != 0;
    row["protected_content_masked"] = bool(info.ProtectedContentMaskedOut);
    if (info.LastPresentTime.QuadPart > 0 &&
        std::uint64_t(info.LastPresentTime.QuadPart) <= returned)
      row["reported_present_age_ms"] =
          milliseconds(returned, info.LastPresentTime.QuadPart);
    try {
      if (validate) {
        if (!own_roi())
          throw std::runtime_error(
              "own pattern ROI is not exclusively visible");
        ComPtr<ID3D11Texture2D> texture;
        checked(resource.As(&texture), "Query acquired texture");
        D3D11_TEXTURE2D_DESC texture_desc{};
        texture->GetDesc(&texture_desc);
        row["format"] = static_cast<unsigned>(texture_desc.Format);
        row["texture_width"] = texture_desc.Width;
        row["texture_height"] = texture_desc.Height;
        if (desc.Rotation != DXGI_MODE_ROTATION_IDENTITY &&
            desc.Rotation != DXGI_MODE_ROTATION_UNSPECIFIED)
          throw std::runtime_error(
              "rotated monitor not supported by this ROI probe");
        RECT rect{};
        GetWindowRect(window, &rect);
        const LONG x = rect.left - desc.DesktopCoordinates.left + 16,
                   y = rect.top - desc.DesktopCoordinates.top + 16;
        if (x < 0 || y < 0 || x + 160 > static_cast<LONG>(texture_desc.Width) ||
            y + 96 > static_cast<LONG>(texture_desc.Height))
          throw std::runtime_error("ROI outside texture");
        if (staging_format != texture_desc.Format) {
          staging.Reset();
          auto sd = texture_desc;
          sd.Width = 160;
          sd.Height = 96;
          sd.MipLevels = 1;
          sd.ArraySize = 1;
          sd.SampleDesc = {1, 0};
          sd.Usage = D3D11_USAGE_STAGING;
          sd.BindFlags = 0;
          sd.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
          sd.MiscFlags = 0;
          checked(device->CreateTexture2D(&sd, nullptr, staging.GetAddressOf()),
                  "Create ROI staging");
          staging_format = texture_desc.Format;
        }
        const D3D11_BOX box{
            static_cast<UINT>(x),       static_cast<UINT>(y),      0,
            static_cast<UINT>(x + 160), static_cast<UINT>(y + 96), 1};
        auto copy_started = ticks();
        context->CopySubresourceRegion(staging.Get(), 0, 0, 0, 0, texture.Get(),
                                       0, &box);
        D3D11_MAPPED_SUBRESOURCE mapped{};
        checked(context->Map(staging.Get(), 0, D3D11_MAP_READ, 0, &mapped),
                "Map actual ROI");
        auto mapped_at = ticks();
        struct unmap_guard {
          ID3D11DeviceContext *context;
          ID3D11Texture2D *texture;
          ~unmap_guard() { context->Unmap(texture, 0); }
        } unmap{context.Get(), staging.Get()};
        const unsigned bpp =
            texture_desc.Format == DXGI_FORMAT_R16G16B16A16_FLOAT ? 8 : 4;
        std::uint64_t hash = 1469598103934665603ULL;
        std::array<std::array<double, 3>, 4> means{};
        for (int i = 0; i < 4; ++i) {
          const int cx = (i % 2 ? 144 : 48) - 16, cy = (i / 2 ? 96 : 32) - 16;
          for (int yy = cy - 2; yy <= cy + 2; ++yy)
            for (int xx = cx - 2; xx <= cx + 2; ++xx) {
              const auto data =
                  static_cast<const unsigned char *>(mapped.pData) +
                  yy * mapped.RowPitch + xx * bpp;
              auto rgb = pixel(data, texture_desc.Format);
              for (int c = 0; c < 3; ++c)
                means[i][c] += rgb[c] / 25.0;
              for (unsigned byte = 0; byte < bpp; ++byte) {
                hash ^= data[byte];
                hash *= 1099511628211ULL;
              }
            }
        }
        bool valid = true;
        for (int i = 0; i < 3; ++i) {
          for (int c = 0; c < 3; ++c)
            if (!std::isfinite(means[i][c]))
              valid = false;
          for (int other = 0; other < 3; ++other)
            if (other != i && means[i][i] <= means[i][other] * 1.5 + .02)
              valid = false;
        }
        for (auto value : means[3])
          if (!std::isfinite(value) || value < .05)
            valid = false;
        row["own_pattern_valid"] = valid;
        row["own_pattern_channel_means"] = means;
        row["sample_hash_fnv64"] = std::to_string(hash);
        row["same_pattern_samples_as_previous"] =
            previous_roi_hash != 0 && hash == previous_roi_hash;
        previous_roi_hash = hash;
        row["roi_cpu_copy_width"] = 160;
        row["roi_cpu_copy_height"] = 96;
        row["roi_readback_ms"] = milliseconds(mapped_at, copy_started);
        row["actual_roi_mapped_qpc_ns"] = std::to_string(ns(mapped_at));
        row["cpu_pixels_saved"] = false;
      }
    } catch (const std::exception &e) {
      row["validation_error"] = e.what();
    }
    resource.Reset();
    auto release_started = ticks();
    auto release_hr = dup->ReleaseFrame();
    release.held = false;
    row["release_hr"] = hr_text(release_hr);
    row["release_ms"] = milliseconds(ticks(), release_started);
    return row;
  }
};

int main(int argc, char **argv) {
  int count = 24, monitor = 0;
  UINT interval_ms = 250, timeout_ms = 150;
  try {
    for (int i = 1; i < argc; ++i) {
      std::string key = argv[i];
      if (i + 1 >= argc)
        throw std::runtime_error("missing argument");
      std::string value = argv[++i];
      if (key == "--count")
        count = std::stoi(value);
      else if (key == "--monitor")
        monitor = value == "primary" ? 0 : std::stoi(value);
      else if (key == "--interval-ms")
        interval_ms = std::stoul(value);
      else if (key == "--timeout-ms")
        timeout_ms = std::stoul(value);
      else
        throw std::runtime_error("unknown argument");
    }
    if (count < 20 || count > 40 || monitor < 0 || monitor > 16 ||
        interval_ms > 1000 || timeout_ms < 1 || timeout_ms > 1000)
      throw std::runtime_error("bounded argument range");
    const auto dpi = SetThreadDpiAwarenessContext(
        DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    struct dpi_restore {
      DPI_AWARENESS_CONTEXT previous;
      ~dpi_restore() {
        if (previous)
          SetThreadDpiAwarenessContext(previous);
      }
    } restore_dpi{dpi};
    ComPtr<IDXGIFactory1> factory;
    checked(CreateDXGIFactory1(IID_PPV_ARGS(factory.GetAddressOf())),
            "Create DXGI factory");
    experiment probe;
    int ordinal = 0;
    for (UINT ai = 0; !probe.output; ++ai) {
      ComPtr<IDXGIAdapter1> adapter;
      if (factory->EnumAdapters1(ai, adapter.GetAddressOf()) ==
          DXGI_ERROR_NOT_FOUND)
        break;
      for (UINT oi = 0; !probe.output; ++oi) {
        ComPtr<IDXGIOutput> output;
        if (adapter->EnumOutputs(oi, output.GetAddressOf()) ==
            DXGI_ERROR_NOT_FOUND)
          break;
        DXGI_OUTPUT_DESC desc{};
        checked(output->GetDesc(&desc), "Output desc");
        if (!desc.AttachedToDesktop)
          continue;
        ++ordinal;
        MONITORINFO info{};
        info.cbSize = sizeof(info);
        GetMonitorInfoW(desc.Monitor, &info);
        if ((monitor == 0 && !(info.dwFlags & MONITORINFOF_PRIMARY)) ||
            (monitor != 0 && ordinal != monitor))
          continue;
        checked(output.As(&probe.output), "Output5 required");
        probe.adapter = adapter;
        probe.desc = desc;
      }
    }
    if (!probe.output)
      throw std::runtime_error("requested desktop output unavailable");
    probe.adapter.As(&probe.adapter3);
    const D3D_FEATURE_LEVEL levels[]{D3D_FEATURE_LEVEL_11_1,
                                     D3D_FEATURE_LEVEL_11_0};
    D3D_FEATURE_LEVEL selected{};
    checked(D3D11CreateDevice(probe.adapter.Get(), D3D_DRIVER_TYPE_UNKNOWN,
                              nullptr, D3D11_CREATE_DEVICE_BGRA_SUPPORT, levels,
                              2, D3D11_SDK_VERSION, probe.device.GetAddressOf(),
                              &selected, probe.context.GetAddressOf()),
            "D3D11 device");
    auto instance = GetModuleHandleW(nullptr);
    const auto name = L"Quest3DDxgiSnapshotPattern";
    WNDCLASSW cls{};
    cls.lpfnWndProc = pattern_proc;
    cls.hInstance = instance;
    cls.lpszClassName = name;
    if (!RegisterClassW(&cls))
      throw std::runtime_error("Register own pattern");
    fixture_state state;
    probe.window = CreateWindowExW(
        WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW, name,
        L"Quest3D DXGI snapshot fixture", WS_POPUP | WS_VISIBLE,
        probe.desc.DesktopCoordinates.left + 64,
        probe.desc.DesktopCoordinates.top + 64, 192, 128, nullptr, nullptr,
        instance, &state);
    struct window_cleanup {
      HWND window;
      HINSTANCE instance;
      const wchar_t *name;
      ~window_cleanup() {
        if (IsWindow(window))
          DestroyWindow(window);
        UnregisterClassW(name, instance);
      }
    } cleanup{probe.window, instance, name};
    if (!probe.window)
      throw std::runtime_error("Create own pattern");
    UpdateWindow(probe.window);
    DwmFlush();
    pause_messages(300);
    std::wstring wide_name(probe.desc.DeviceName);
    std::string display_name(wide_name.begin(), wide_name.end());
    json metadata{
        {"kind", "metadata"},
        {"version", 1},
        {"monitor_ordinal", ordinal},
        {"display_name", display_name},
        {"source_hmonitor",
         std::to_string(reinterpret_cast<std::uintptr_t>(probe.desc.Monitor))},
        {"physical_bounds",
         {probe.desc.DesktopCoordinates.left, probe.desc.DesktopCoordinates.top,
          probe.desc.DesktopCoordinates.right,
          probe.desc.DesktopCoordinates.bottom}},
        {"rotation", probe.desc.Rotation},
        {"qpc_frequency", frequency()},
        {"same_d3d_device", true},
        {"fixture_size", {192, 128}},
        {"roi_only_cpu_readback", true},
        {"count", count},
        {"interval_ms", interval_ms},
        {"format_list", {10, 87, 88, 28}},
        {"memory_after_device", memory(probe.adapter3.Get())}};
    ComPtr<IDXGIOutput6> output6;
    if (SUCCEEDED(probe.output.As(&output6))) {
      DXGI_OUTPUT_DESC1 desc1{};
      if (SUCCEEDED(output6->GetDesc1(&desc1))) {
        metadata["color_space"] = desc1.ColorSpace;
        metadata["bits_per_color"] = desc1.BitsPerColor;
      }
    }
    emit(metadata);
    // Persistent-reader control: absence is recorded, never converted into
    // success.
    double longest_quiet_ms = 0;
    {
      ComPtr<IDXGIOutputDuplication> persistent;
      auto hr = probe.open(persistent);
      emit({{"kind", "persistent_open"}, {"hr", hr_text(hr)}});
      if (SUCCEEDED(hr)) {
        std::uint64_t quiet_started = 0;
        for (int i = 0; i < 20; ++i) {
          auto row = probe.acquire(persistent.Get(), 100, i == 0);
          row["kind"] = "persistent";
          row["index"] = i;
          if (row.value("timeout", false)) {
            auto started_ns =
                std::stoull(row["acquire_started_qpc_ns"].get<std::string>());
            auto returned_ns =
                std::stoull(row["acquire_returned_qpc_ns"].get<std::string>());
            if (!quiet_started)
              quiet_started = started_ns;
            auto quiet_ms = double(returned_ns - quiet_started) / 1000000.0;
            longest_quiet_ms = std::max(longest_quiet_ms, quiet_ms);
            row["consecutive_timeout_span_ms"] = quiet_ms;
          } else {
            quiet_started = 0;
          }
          emit(row);
          if (longest_quiet_ms >= 550)
            break;
          pause_messages(10);
        }
        persistent.Reset();
        probe.context->Flush();
        pause_messages(200);
      }
    }
    emit({{"kind", "baseline"},
          {"memory", memory(probe.adapter3.Get())},
          {"longest_persistent_timeout_span_ms", longest_quiet_ms},
          {"pattern_paints", state.paints}});
    for (int iteration = 0; iteration < count; ++iteration) {
      if (!probe.own_roi())
        throw std::runtime_error("Own fixture lost before snapshot");
      const auto iteration_started = ticks();
      ComPtr<IDXGIOutputDuplication> dup;
      auto open_started = ticks();
      auto hr = probe.open(dup);
      auto opened = ticks();
      json row{{"kind", "snapshot"},
               {"index", iteration},
               {"open_hr", hr_text(hr)},
               {"open_ms", milliseconds(opened, open_started)},
               {"memory_before_acquire", memory(probe.adapter3.Get())}};
      if (SUCCEEDED(hr)) {
        row.update(probe.acquire(dup.Get(), timeout_ms, true));
        if (row.value("acquire_hr", "") == "0x00000000" &&
            row.value("own_pattern_valid", false) &&
            row.value("release_hr", "") == "0x00000000")
          ++probe.success;
        else if (row.value("timeout", false))
          ++probe.timeout;
        else {
          ++probe.failed;
          if (row.contains("own_pattern_valid") &&
              !row["own_pattern_valid"].get<bool>())
            ++probe.pixel_failures;
        }
      } else
        ++probe.failed;
      auto close_started = ticks();
      dup.Reset();
      probe.context->Flush();
      row["close_ms"] = milliseconds(ticks(), close_started);
      row["iteration_ms"] = milliseconds(ticks(), iteration_started);
      pause_messages(interval_ms);
      row["memory_after_close_and_wait"] = memory(probe.adapter3.Get());
      row["pattern_paints"] = state.paints;
      emit(row);
    }
    probe.staging.Reset();
    probe.context->ClearState();
    probe.context->Flush();
    pause_messages(1000);
    emit({{"kind", "summary"},
          {"successful_pattern_snapshots", probe.success},
          {"timeouts", probe.timeout},
          {"failures", probe.failed},
          {"pixel_failures", probe.pixel_failures},
          {"longest_persistent_timeout_span_ms", longest_quiet_ms},
          {"pattern_paints", state.paints},
          {"final_memory", memory(probe.adapter3.Get())},
          {"same_device_retained", true},
          {"runtime_integrated", false},
          {"zero_copy_ai_integrated", false},
          {"os_input_injected", false},
          {"source_timestamp_rewritten", false}});
    return probe.success == static_cast<unsigned>(count) &&
                   probe.timeout == 0 && probe.failed == 0
               ? 0
               : 2;
  } catch (const std::exception &e) {
    emit({{"kind", "fatal"},
          {"error", e.what()},
          {"os_input_injected", false},
          {"pixels_saved", false}});
    return 1;
  }
}
