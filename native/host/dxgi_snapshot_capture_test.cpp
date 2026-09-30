// Test the same implementation, including pure geometry/lifetime predicates,
// without synthesizing DXGI success or calling user input/display mutation
// APIs.
#define Q3D_DXGI_TEST
#include "dxgi_snapshot_capture.cpp"
#include <iostream>
#include <latch>
#include <stdexcept>
#include <thread>

int main() {
  int checks = 0;
  auto check = [&](bool value, const char *name) {
    if (!value)
      throw std::runtime_error(name);
    ++checks;
  };
  try {
    check(fresh(1, 501, 1000, 500), "age boundary");
    check(!fresh(1, 502, 1000, 500), "expired");
    check(!fresh(0, 1, 1000, 500), "pointer only/zero present");
    check(!fresh(2, 1, 1000, 500), "future present");
    check(classify(DXGI_ERROR_WAIT_TIMEOUT) == Q3D_DXGI_TIMEOUT,
          "timeout status");
    check(classify(E_ACCESSDENIED) == Q3D_DXGI_ACCESS_DENIED, "access status");
    check(classify(DXGI_ERROR_ACCESS_LOST) == Q3D_DXGI_SOURCE_CHANGED,
          "access lost status");
    check(classify(DXGI_ERROR_DEVICE_REMOVED) == Q3D_DXGI_DEVICE_LOST,
          "removed status");
    check(classify(DXGI_ERROR_DEVICE_RESET) == Q3D_DXGI_DEVICE_LOST,
          "reset status");
    check(classify(DXGI_ERROR_SESSION_DISCONNECTED) == Q3D_DXGI_SOURCE_CHANGED,
          "offline status");
    q3d_dxgi_source source{
        1, sizeof(q3d_dxgi_source), 42, 7, -100, 0, 100, 80, 1, 0, {}};
    std::memcpy(source.device_name, L"DISPLAYX", 18);
    DXGI_OUTPUT_DESC desc{};
    desc.Monitor = reinterpret_cast<HMONITOR>(42);
    desc.AttachedToDesktop = TRUE;
    desc.DesktopCoordinates = {-100, 0, 0, 80};
    desc.Rotation = DXGI_MODE_ROTATION_IDENTITY;
    std::memcpy(desc.DeviceName, source.device_name,
                sizeof(source.device_name));
    MONITORINFOEXW info{};
    info.rcMonitor = desc.DesktopCoordinates;
    std::memcpy(info.szDevice, source.device_name, sizeof(source.device_name));
    check(source_matches(source, desc, info, LUID{7, 0}, true, true),
          "same source");
    check(!source_matches(source, desc, info, LUID{8, 0}, true, true),
          "adapter change");
    check(!source_matches(source, desc, info, LUID{7, 0}, false, true),
          "DPI change");
    check(!source_matches(source, desc, info, LUID{7, 0}, true, false),
          "factory topology change");
    desc.AttachedToDesktop = FALSE;
    check(!source_matches(source, desc, info, LUID{7, 0}, true, true),
          "offline monitor");
    desc.AttachedToDesktop = TRUE;
    desc.Monitor = reinterpret_cast<HMONITOR>(43);
    check(!source_matches(source, desc, info, LUID{7, 0}, true, true),
          "replaced monitor");
    desc.Monitor = reinterpret_cast<HMONITOR>(42);
    ++desc.DesktopCoordinates.left;
    check(!source_matches(source, desc, info, LUID{7, 0}, true, true),
          "DXGI moved");
    --desc.DesktopCoordinates.left;
    ++info.rcMonitor.right;
    check(!source_matches(source, desc, info, LUID{7, 0}, true, true),
          "Win32 resized");
    --info.rcMonitor.right;
    desc.Rotation = DXGI_MODE_ROTATION_ROTATE90;
    check(!source_matches(source, desc, info, LUID{7, 0}, true, true),
          "rotation change");
    desc.Rotation = DXGI_MODE_ROTATION_IDENTITY;
    desc.DeviceName[0] = L'Z';
    check(!source_matches(source, desc, info, LUID{7, 0}, true, true),
          "device name change");
    unsigned char pixels[8]{1, 2, 3, 4, 5, 6, 7, 8};
    q3d_dxgi_frame frame{};
    q3d_dxgi_error error{};
    std::memset(&frame, 0xAB, sizeof(frame));
    check(q3d_dxgi_snapshot(987654321, pixels, sizeof(pixels), 1, 500, &frame,
                            sizeof(frame), &error) == Q3D_DXGI_INVALID_HANDLE,
          "invalid handle");
    check(frame.version == 0 && frame.copied_bytes == 0 && pixels[0] == 1,
          "failure metadata/pixels");
    check(q3d_dxgi_snapshot(0, pixels, 0, 1, 500, &frame, sizeof(frame),
                            &error) == Q3D_DXGI_INVALID_ARGUMENT,
          "zero capacity");
    check(q3d_dxgi_snapshot(0, pixels, 8, 0, 500, &frame, sizeof(frame),
                            &error) == Q3D_DXGI_INVALID_ARGUMENT,
          "unbounded timeout");
    check(q3d_dxgi_snapshot(0, pixels, 8, 1, 501, &frame, sizeof(frame),
                            &error) == Q3D_DXGI_INVALID_ARGUMENT,
          "freshness relaxation");
    check(q3d_dxgi_close(0, &error) == Q3D_DXGI_INVALID_HANDLE,
          "invalid close");
    uint64_t handle = 123;
    check(q3d_dxgi_open(0, &handle, &source, sizeof(source), &error) ==
                  Q3D_DXGI_INVALID_ARGUMENT &&
              !handle && !source.version,
          "invalid monitor");
    std::shared_ptr<session> orphan;
    std::thread owner([&] { orphan = std::make_shared<session>(); });
    owner.join();
    check(orphan->owner_dead() && !orphan->is_owner(),
          "actual owner thread death");
    orphan->thread = GetCurrentThreadId(); // Simulate numeric TID reuse, not
                                           // kernel identity.
    check(!orphan->is_owner(),
          "same TID does not reuse kernel thread identity");
    {
      std::lock_guard lock(registry_mutex);
      registry.emplace(9001, orphan);
    }
    check(q3d_dxgi_snapshot(9001, pixels, sizeof(pixels), 1, 500, &frame,
                            sizeof(frame), &error) == Q3D_DXGI_OWNER_DEAD,
          "dead owner snapshot rejected");
    check(q3d_dxgi_close(9001, &error) == Q3D_DXGI_OK && !orphan->owner_thread,
          "dead owner resources explicitly reaped");
    auto closing = std::make_shared<session>();
    {
      std::lock_guard lock(registry_mutex);
      registry.emplace(9002, closing);
    }
    std::latch looked_up(1), close_done(1);
    DWORD observed_teardown = 0;
    std::thread foreign([&] {
      auto held = lookup(9002);
      looked_up.count_down();
      close_done.wait();
      observed_teardown = held->teardown_thread;
    });
    looked_up.wait();
    check(q3d_dxgi_close(9002, &error) == Q3D_DXGI_OK && !closing->owner_thread,
          "close tears down under owner lock");
    close_done.count_down();
    foreign.join();
    check(observed_teardown == GetCurrentThreadId(),
          "foreign last reference cannot move COM teardown");
    std::cout << "{\"checks\":" << checks
              << ",\"passed\":true,\"actual_capture\":false,\"display_"
                 "changes\":false}\n";
    return 0;
  } catch (const std::exception &e) {
    std::cout << "failed: " << e.what() << '\n';
    return 1;
  }
}
