// Reuse the already measured standalone test window, color reader and DXGI
// memory instrumentation. Its original executable entry point is never run.
// All measured full-frame snapshots below call the separate public DLL ABI.
#define main q3d_roi_reference_entry_not_called
#include "dxgi-snapshot-probe.cpp"
#undef main
#include "dxgi_snapshot_capture.h"
#include <thread>

namespace {
json source_json(const q3d_dxgi_source &s) {
  return {{"hmonitor", std::to_string(s.hmonitor)},
          {"adapter_luid", std::to_string(s.adapter_luid)},
          {"bounds", {s.left, s.top, s.width, s.height}},
          {"rotation", s.rotation}};
}
json error_json(const q3d_dxgi_error &e) {
  return {{"status", e.status},
          {"stage", e.stage},
          {"hr", hr_text(e.hresult)},
          {"required_capacity", e.required_capacity}};
}
bool verify_owned_pattern(HWND window, const q3d_dxgi_frame &f,
                          const std::vector<unsigned char> &pixels) {
  RECT rect{};
  DWORD pid = 0;
  GetWindowThreadProcessId(window, &pid);
  if (!IsWindowVisible(window) || pid != GetCurrentProcessId() ||
      !GetWindowRect(window, &rect) || rect.right - rect.left != 192 ||
      rect.bottom - rect.top != 128)
    return false;
  for (int i = 0; i < 4; ++i) {
    const int x = rect.left + (i % 2 ? 144 : 48),
              y = rect.top + (i / 2 ? 96 : 32);
    if (WindowFromPoint(POINT{x, y}) != window)
      return false;
    const int tx = x - f.source.left, ty = y - f.source.top;
    if (tx < 2 || ty < 2 || tx + 2 >= int(f.source.width) ||
        ty + 2 >= int(f.rows))
      return false;
    for (int yy = ty - 2; yy <= ty + 2; ++yy)
      for (int xx = tx - 2; xx <= tx + 2; ++xx) {
        auto values = pixel(pixels.data() + uint64_t(yy) * f.row_bytes +
                                uint64_t(xx) * f.bytes_per_pixel,
                            DXGI_FORMAT(f.format));
        for (auto value : values)
          if (!std::isfinite(value))
            return false;
        if (i < 3) {
          for (int c = 0; c < 3; ++c)
            if (c != i && values[i] <= values[c] * 1.5 + .02)
              return false;
        } else {
          for (auto value : values)
            if (value < .05)
              return false;
        }
      }
  }
  return true;
}
} // namespace

int main(int argc, char **argv) {
  try {
    int ordinal_wanted = argc > 1 ? std::stoi(argv[1]) : 2;
    int count = argc > 2 ? std::stoi(argv[2]) : 40;
    if (ordinal_wanted < 1 || ordinal_wanted > 16 || count < 20 || count > 40)
      throw std::runtime_error("bounded monitor/count");
    auto previous = SetThreadDpiAwarenessContext(
        DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    struct dpi_guard {
      DPI_AWARENESS_CONTEXT value;
      ~dpi_guard() {
        if (value)
          SetThreadDpiAwarenessContext(value);
      }
    } restore{previous};
    ComPtr<IDXGIFactory1> factory;
    checked(CreateDXGIFactory1(IID_PPV_ARGS(factory.GetAddressOf())),
            "factory");
    experiment control;
    int ordinal = 0;
    for (UINT ai = 0; !control.output; ++ai) {
      ComPtr<IDXGIAdapter1> adapter;
      if (factory->EnumAdapters1(ai, adapter.GetAddressOf()) ==
          DXGI_ERROR_NOT_FOUND)
        break;
      for (UINT oi = 0; !control.output; ++oi) {
        ComPtr<IDXGIOutput> output;
        if (adapter->EnumOutputs(oi, output.GetAddressOf()) ==
            DXGI_ERROR_NOT_FOUND)
          break;
        DXGI_OUTPUT_DESC desc{};
        checked(output->GetDesc(&desc), "output");
        if (!desc.AttachedToDesktop)
          continue;
        if (++ordinal != ordinal_wanted)
          continue;
        control.adapter = adapter;
        control.desc = desc;
        checked(output.As(&control.output), "output5");
      }
    }
    if (!control.output)
      throw std::runtime_error("monitor missing");
    control.adapter.As(&control.adapter3);
    auto instance = GetModuleHandleW(nullptr);
    const wchar_t *name = L"Quest3DDxgiFullFrameFixture";
    WNDCLASSW cls{};
    cls.lpfnWndProc = pattern_proc;
    cls.hInstance = instance;
    cls.lpszClassName = name;
    if (!RegisterClassW(&cls))
      throw std::runtime_error("register fixture");
    fixture_state state;
    control.window = CreateWindowExW(
        WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW, name,
        L"Quest3D full-frame DXGI fixture", WS_POPUP | WS_VISIBLE,
        control.desc.DesktopCoordinates.left + 64,
        control.desc.DesktopCoordinates.top + 64, 192, 128, nullptr, nullptr,
        instance, &state);
    struct window_guard {
      HWND window;
      HINSTANCE instance;
      const wchar_t *name;
      ~window_guard() {
        if (IsWindow(window))
          DestroyWindow(window);
        UnregisterClassW(name, instance);
      }
    } cleanup{control.window, instance, name};
    if (!control.window)
      throw std::runtime_error("fixture");
    UpdateWindow(control.window);
    DwmFlush();
    pause_messages(300);
    // Real no-update control, closed before the DLL creates its single device.
    checked(D3D11CreateDevice(control.adapter.Get(), D3D_DRIVER_TYPE_UNKNOWN,
                              nullptr, D3D11_CREATE_DEVICE_BGRA_SUPPORT,
                              nullptr, 0, D3D11_SDK_VERSION,
                              control.device.GetAddressOf(), nullptr,
                              control.context.GetAddressOf()),
            "control device");
    ComPtr<IDXGIOutputDuplication> persistent;
    checked(control.open(persistent), "persistent control");
    double quiet_wait = 0, max_quiet_wait = 0;
    for (int i = 0; i < 20; ++i) {
      auto row = control.acquire(persistent.Get(), 100, i == 0);
      row["kind"] = "persistent_control";
      row["index"] = i;
      if (row.value("timeout", false))
        quiet_wait += row["acquire_ms"].get<double>();
      else
        quiet_wait = 0;
      max_quiet_wait = std::max(max_quiet_wait, quiet_wait);
      emit(row);
      if (max_quiet_wait >= 550)
        break;
      pause_messages(10);
    }
    persistent.Reset();
    control.staging.Reset();
    control.context->ClearState();
    control.context->Flush();
    control.context.Reset();
    control.device.Reset();
    pause_messages(250);
    q3d_dxgi_error error{};
    q3d_dxgi_source source{};
    uint64_t handle = 0;
    auto status = q3d_dxgi_open(uint64_t(uintptr_t(control.desc.Monitor)),
                                &handle, &source, sizeof(source), &error);
    emit({{"kind", "open"},
          {"error", error_json(error)},
          {"source", source_json(source)}});
    if (status != Q3D_DXGI_OK)
      throw std::runtime_error("DLL open failed");
    struct dll_guard {
      uint64_t handle;
      ~dll_guard() {
        if (handle)
          q3d_dxgi_close(handle, nullptr);
      }
    } close_on_error{handle};
    const uint64_t capacity = uint64_t(source.width) * source.height * 8;
    std::vector<unsigned char> buffer(capacity + 64, 0xCD);
    const auto buffer_address = buffer.data();
    q3d_dxgi_frame frame{};
    // The insufficient-buffer call must release its acquired frame and cannot
    // write its canary. A subsequent normal snapshot proves recovery.
    unsigned char canary[8];
    std::memset(canary, 0xAB, sizeof(canary));
    status = q3d_dxgi_snapshot(handle, canary, sizeof(canary), 150, 500, &frame,
                               sizeof(frame), &error);
    bool capacity_ok = status == Q3D_DXGI_CAPACITY && !frame.version &&
                       !frame.copied_bytes &&
                       error.required_capacity > sizeof(canary);
    for (auto byte : canary)
      capacity_ok = capacity_ok && byte == 0xAB;
    emit({{"kind", "capacity_test"},
          {"passed", capacity_ok},
          {"error", error_json(error)}});
    if (!capacity_ok)
      throw std::runtime_error("capacity rejection");
    int wrong_thread_snapshot = -1, wrong_thread_close = -1;
    std::thread other([&] {
      q3d_dxgi_frame f{};
      q3d_dxgi_error e{};
      wrong_thread_snapshot = q3d_dxgi_snapshot(handle, buffer.data(), capacity,
                                                150, 500, &f, sizeof(f), &e);
      wrong_thread_close = q3d_dxgi_close(handle, &e);
    });
    other.join();
    if (wrong_thread_snapshot != Q3D_DXGI_WRONG_THREAD ||
        wrong_thread_close != Q3D_DXGI_WRONG_THREAD)
      throw std::runtime_error("thread ownership");
    q3d_dxgi_source invalid_source{};
    uint64_t invalid_handle = 123;
    auto invalid_status =
        q3d_dxgi_open(UINT64_MAX, &invalid_handle, &invalid_source,
                      sizeof(invalid_source), &error);
    if (invalid_status != Q3D_DXGI_SOURCE_CHANGED || invalid_handle ||
        invalid_source.version)
      throw std::runtime_error("invalid monitor metadata");
    emit({{"kind", "api_rejection_tests"},
          {"wrong_thread_snapshot", wrong_thread_snapshot},
          {"wrong_thread_close", wrong_thread_close},
          {"missing_monitor_status", invalid_status},
          {"passed", true}});
    status = q3d_dxgi_snapshot(handle, buffer.data(), capacity, 150, 500,
                               &frame, sizeof(frame), &error);
    if (status != Q3D_DXGI_OK ||
        !verify_owned_pattern(control.window, frame, buffer))
      throw std::runtime_error("warm snapshot");
    pause_messages(250);
    emit({{"kind", "baseline"},
          {"memory", memory(control.adapter3.Get())},
          {"source", source_json(source)},
          {"persistent_timeout_wait_sum_ms", max_quiet_wait},
          {"warm_frame_id", frame.frame_id},
          {"whole_frame_cpu_capacity", capacity},
          {"caller_buffer_reused", true},
          {"pattern_paints", state.paints}});
    uint64_t previous_frame = frame.frame_id,
             previous_present = frame.last_present_qpc;
    unsigned successes = 0;
    for (int i = 0; i < count; ++i) {
      if (!control.own_roi())
        throw std::runtime_error("fixture lost");
      const auto begin = ticks();
      status = q3d_dxgi_snapshot(handle, buffer.data(), capacity, 150, 500,
                                 &frame, sizeof(frame), &error);
      const auto ended = ticks();
      bool valid =
          status == Q3D_DXGI_OK && frame.version == 1 &&
          frame.size == sizeof(frame) &&
          std::memcmp(&frame.source, &source, sizeof(source)) == 0 &&
          frame.frame_id == previous_frame + 1 &&
          frame.last_present_qpc > previous_present &&
          frame.last_present_qpc <= ended &&
          milliseconds(ended, frame.last_present_qpc) <= 500 &&
          frame.copied_bytes == uint64_t(frame.row_bytes) * frame.rows &&
          frame.row_bytes == source.width * frame.bytes_per_pixel &&
          frame.rows == source.height &&
          verify_owned_pattern(control.window, frame, buffer) &&
          buffer.data() == buffer_address;
      for (uint64_t p = capacity; p < buffer.size(); ++p)
        valid = valid && buffer[p] == 0xCD;
      json row{
          {"kind", "snapshot"},
          {"index", i},
          {"error", error_json(error)},
          {"valid", valid},
          {"call_ms", milliseconds(ended, begin)},
          {"frame_id", frame.frame_id},
          {"format", frame.format},
          {"row_bytes", frame.row_bytes},
          {"copied_bytes", frame.copied_bytes},
          {"source", source_json(frame.source)},
          {"last_present_qpc", std::to_string(frame.last_present_qpc)},
          {"acquire_started_qpc", std::to_string(frame.acquire_started_qpc)},
          {"acquire_returned_qpc", std::to_string(frame.acquire_returned_qpc)},
          {"map_completed_qpc", std::to_string(frame.map_completed_qpc)},
          {"copy_completed_qpc", std::to_string(frame.copy_completed_qpc)},
          {"qpc_frequency", frame.qpc_frequency},
          {"pixels_saved", false}};
      if (status == Q3D_DXGI_OK) {
        row["acquire_ms"] =
            milliseconds(frame.acquire_returned_qpc, frame.acquire_started_qpc);
        row["texture_to_mapped_ms"] =
            milliseconds(frame.map_completed_qpc, frame.acquire_returned_qpc);
        row["mapped_to_caller_copy_ms"] =
            milliseconds(frame.copy_completed_qpc, frame.map_completed_qpc);
        row["present_age_at_return_ms"] =
            milliseconds(ended, frame.last_present_qpc);
        previous_frame = frame.frame_id;
        previous_present = frame.last_present_qpc;
      }
      if (valid)
        ++successes;
      pause_messages(250);
      row["memory_after_snapshot_wait"] = memory(control.adapter3.Get());
      row["pattern_paints"] = state.paints;
      emit(row);
    }
    emit({{"kind", "retained_device_final"},
          {"memory", memory(control.adapter3.Get())}});
    status = q3d_dxgi_close(handle, &error);
    close_on_error.handle = 0;
    if (status != Q3D_DXGI_OK)
      throw std::runtime_error("close failure");
    if (q3d_dxgi_snapshot(handle, buffer.data(), capacity, 150, 500, &frame,
                          sizeof(frame), &error) != Q3D_DXGI_INVALID_HANDLE ||
        frame.version)
      throw std::runtime_error("closed handle");
    uint64_t reopened = 0;
    q3d_dxgi_source reopened_source{};
    if (q3d_dxgi_open(source.hmonitor, &reopened, &reopened_source,
                      sizeof(reopened_source), &error) != Q3D_DXGI_OK ||
        reopened <= handle)
      throw std::runtime_error("new handle identity");
    if (q3d_dxgi_close(reopened, &error) != Q3D_DXGI_OK)
      throw std::runtime_error("reopen close");
    pause_messages(1000);
    emit({{"kind", "summary"},
          {"count", count},
          {"successful_snapshots", successes},
          {"pattern_paints", state.paints},
          {"persistent_timeout_wait_sum_ms", max_quiet_wait},
          {"memory_after_close", memory(control.adapter3.Get())},
          {"closed_handle_rejected", true},
          {"reopen_new_handle", true},
          {"caller_buffer_reused", true},
          {"whole_frame_cpu_copied", true},
          {"pixels_saved", false},
          {"os_input_injected", false},
          {"display_settings_changed", false},
          {"cuda_or_ai_integrated", false},
          {"product_runtime_integrated", false}});
    return successes == unsigned(count) ? 0 : 2;
  } catch (const std::exception &e) {
    emit({{"kind", "fatal"}, {"error", e.what()}, {"pixels_saved", false}});
    return 1;
  }
}
