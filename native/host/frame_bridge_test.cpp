// Windows integration tests exercise actual named mappings, mutexes, and events.
#include "quest3d_frame.h"
#include "quest3d_input.h"
#include <atomic>
#include <cstring>
#include <iostream>
#include <stdexcept>
#include <thread>
#include <limits>

void check(bool condition, const char *message) {
  if (!condition) {
    throw std::runtime_error(message);
  }
}

struct publisher {
  quest3d::object_names names;
  HANDLE mapping = nullptr;
  HANDLE mutex = nullptr;
  HANDLE ready = nullptr;
  std::uint8_t *view = nullptr;
  explicit publisher(quest3d::object_names value): names(std::move(value)) {
    mapping = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0,
                                static_cast<DWORD>(quest3d::mapping_capacity), names.mapping.c_str());
    mutex = CreateMutexW(nullptr, FALSE, names.mutex.c_str());
    ready = CreateEventW(nullptr, FALSE, FALSE, names.ready.c_str());
    check(mapping && mutex && ready, "create publisher objects");
    view = static_cast<std::uint8_t *>(MapViewOfFile(mapping, FILE_MAP_WRITE, 0, 0, quest3d::mapping_capacity));
    check(view != nullptr, "map publisher");
  }
  ~publisher() {
    if (view) UnmapViewOfFile(view);
    for (auto handle : {ready, mutex, mapping}) if (handle) CloseHandle(handle);
  }
  void publish(const quest3d::frame_header &header, std::uint8_t value) {
    check(WaitForSingleObject(mutex, 1000) == WAIT_OBJECT_0, "publisher lock");
    std::memset(view + sizeof(header), value, header.payload_bytes);
    std::memcpy(view, &header, sizeof(header));
    ReleaseMutex(mutex);
    SetEvent(ready);
  }
};

quest3d::frame_header valid_frame() {
  quest3d::frame_header h {};
  h.magic = {'Q', '3', 'D', 'F', 'R', 'M', '2', '\0'};
  h.version = 2;
  h.header_bytes = 128;
  h.width = 64;
  h.height = 32;
  h.stride = h.width * 4;
  h.format = 1;
  h.frame_id = 1;
  h.capture_qpc_ns = quest3d::qpc_nanoseconds();
  h.publish_qpc_ns = h.capture_qpc_ns;
  h.payload_bytes = h.stride * h.height;
  h.source_width = 32;
  h.source_height = 32;
  h.stream_epoch = 1;
  h.flags = 1;
  h.content_width = 32;
  h.content_height = 32;
  return h;
}

int main() {
  try {
    auto h = valid_frame();
    check(quest3d::valid_header(h), "valid v2 layout");
    auto bad = h;
    bad.payload_bytes--;
    check(!quest3d::valid_header(bad), "reject mismatched payload");
    bad = h; bad.width = 0xffffffffU;
    check(!quest3d::valid_header(bad), "reject dimensions before overflow");
    bad = h; bad.height = 2161;
    check(!quest3d::valid_header(bad), "reject oversized height");
    bad = h; bad.format = 2;
    check(!quest3d::valid_header(bad), "reject unknown format");
    bad = h; bad.flags = 16;
    check(!quest3d::valid_header(bad), "reject unknown flags");
    bad = h; bad.header_bytes = 64;
    check(!quest3d::valid_header(bad), "reject prior header layout");
    bad = h; bad.capture_qpc_ns++;
    check(!quest3d::valid_header(bad), "reject reversed timestamps");
    bad = h; bad.version = 1;
    check(!quest3d::valid_header(bad), "reject v1 with missing content geometry");
    bad = h; bad.content_left = 32;
    check(!quest3d::valid_header(bad), "reject content outside eye canvas");
    bad = h; bad.content_width = 0xffffffffU;
    check(!quest3d::valid_header(bad), "reject overflowing content rectangle");
    bad = h; bad.reserved[15] = 1;
    check(!quest3d::valid_header(bad), "reject unknown reserved content");
    bad = h; bad.flags |= 8;
    check(quest3d::valid_header(bad), "accept explicit input enabled metadata");
    bad = h; bad.content_width = 0; bad.content_height = 0;
    check(quest3d::valid_header(bad), "allow output-only unspecified content");
    bad.flags |= 8;
    check(!quest3d::valid_header(bad), "input needs explicit content");

    auto geometry = h;
    geometry.source_left = -1600; geometry.source_top = 200;
    geometry.source_width = 1280; geometry.source_height = 720;
    const quest3d::desktop_rect desktop {-1920, -1080, 5760, 3240};
    using space = quest3d::input_space;
    auto point = quest3d::map_absolute(geometry, 0.5, 0.5, space::logical_eye_uv, desktop);
    check(point && point->physical_x == -960 && point->physical_y == 560, "logical center maps to physical ROI center, not SBS seam");
    check(static_cast<int>(point->normalized_x * 5760LL / 65536) == 960, "pixel center Win32 normalization includes negative desktop origin");
    point = quest3d::map_absolute(geometry, 1, 1, space::logical_eye_uv, desktop);
    check(point && point->physical_x == -321 && point->physical_y == 919, "bottom-right remains inside last physical pixel");
    geometry.content_left = 4; geometry.content_top = 4; geometry.content_width = 24; geometry.content_height = 24;
    check(!quest3d::map_absolute(geometry, 0, 0.5, space::logical_eye_uv, desktop), "padding is rejected instead of clamped to a clickable edge");
    auto left = quest3d::map_absolute(geometry, 0.25, 0.5, space::combined_canvas_uv, desktop);
    auto right = quest3d::map_absolute(geometry, 0.75, 0.5, space::combined_canvas_uv, desktop);
    check(left && right && left->physical_x == right->physical_x && left->physical_x == -960, "explicit packed left/right eye centers share source coordinate");
    check(!quest3d::map_absolute(geometry, std::numeric_limits<double>::quiet_NaN(), 0.5, space::logical_eye_uv, desktop), "reject NaN pointer");
    check(!quest3d::map_absolute(geometry, -0.1, 0.5, space::logical_eye_uv, desktop), "reject negative pointer");
    geometry.source_left = 100000;
    check(!quest3d::map_absolute(geometry, 0.5, 0.5, space::logical_eye_uv, desktop), "reject off-desktop source");

    quest3d::input_guard guard;
    check(!guard.observe(h, h.publish_qpc_ns).allowed, "input is disabled by default");
    auto enabled = h; enabled.flags = 9;
    check(!guard.observe(enabled, h.publish_qpc_ns).allowed, "3D raw pointer control is not silently enabled");
    enabled.flags = 11;
    auto decision = guard.observe(enabled, enabled.publish_qpc_ns);
    check(decision.allowed && decision.release_held && !guard.positioned(), "opt-in 2D starts without a bound pointer");
    guard.position(true);
    check(guard.positioned(), "valid absolute point enables buttons and keys");
    check(!guard.observe(enabled, enabled.publish_qpc_ns + 100).release_held, "unchanged geometry does not interrupt drag");
    enabled.geometry_generation++;
    check(guard.observe(enabled, enabled.publish_qpc_ns).release_held && !guard.positioned(), "source generation change releases held state");
    guard.position(true);
    decision = guard.observe(enabled, enabled.publish_qpc_ns + 500000001ULL);
    check(!decision.allowed && decision.release_held && !guard.positioned(), "dead producer is blocked and held state released");
    check(!guard.observe(enabled, enabled.publish_qpc_ns - 1).allowed, "future publication is blocked");
    auto regressed = enabled; regressed.geometry_generation = 0;
    check(!guard.observe(regressed, enabled.publish_qpc_ns).allowed, "same-epoch geometry regression is blocked");
    regressed.stream_epoch++;
    check(guard.observe(regressed, regressed.publish_qpc_ns).allowed, "new epoch permits a restarted generation after release");
    guard.position(true); guard.reset();
    check(!guard.positioned(), "reconnect never retains a prior pointer");
    quest3d::publish_input_geometry(enabled);
    check(quest3d::input_geometry()->geometry_generation == enabled.geometry_generation, "metadata cache preserves source generation");
    quest3d::clear_input_geometry();
    check(!quest3d::input_geometry(), "invalidated cache cannot authorize input");

    const auto suffix = L".Test." + std::to_wstring(GetCurrentProcessId());
    quest3d::object_names names;
    names.mapping += suffix; names.mutex += suffix; names.ready += suffix;
    quest3d::frame_reader reader(names);
    quest3d::frame out;
    check(reader.read(out, 0) == quest3d::read_result::unavailable, "absent writer does not create mapping");
    publisher writer(names);
    check(reader.read(out, 0) == quest3d::read_result::invalid, "zero mapping is not a frame");
    writer.publish(h, 0x31);
    quest3d::frame_reader metadata_reader(names);
    quest3d::frame_header metadata {};
    check(metadata_reader.read_metadata(metadata, 0) == quest3d::read_result::fresh && metadata.width == h.width && metadata.height == h.height, "metadata-only profile uses actual source dimensions");
    check(quest3d::full_sbs_profile(metadata, h.publish_qpc_ns), "valid Full-SBS profile");
    check(!quest3d::full_sbs_profile(metadata, h.publish_qpc_ns - 1), "no future profile");
    check(!quest3d::full_sbs_profile(metadata, h.publish_qpc_ns + 2000000001ULL), "no stale profile");
    metadata.flags = 0;
    check(!quest3d::full_sbs_profile(metadata, h.publish_qpc_ns), "mono source is not advertised as SBS");
    metadata.flags = 3;
    check(quest3d::full_sbs_profile(metadata, h.publish_qpc_ns), "duplicated 2D retains Full-SBS profile");
    metadata.height = 31; metadata.content_height = 31; metadata.payload_bytes = metadata.stride * metadata.height;
    check(!quest3d::full_sbs_profile(metadata, h.publish_qpc_ns), "odd encoded height is not offered to the Quest decoder");
    check(reader.read(out, 0) == quest3d::read_result::fresh, "first complete frame");
    check(out.pixels.front() == 0x31 && out.pixels.back() == 0x31, "payload preserved");
    check(reader.read(out, 0) == quest3d::read_result::unchanged, "no duplicate freshness");
    h.frame_id = 3; writer.publish(h, 0x33);
    check(reader.read(out, 0) == quest3d::read_result::fresh, "latest skips missed frames");
    h.frame_id = 2; writer.publish(h, 0x32);
    check(reader.read(out, 0) == quest3d::read_result::invalid, "reject same-epoch regression");
    h.stream_epoch = 2; h.frame_id = 1; writer.publish(h, 0x40);
    check(reader.read(out, 0) == quest3d::read_result::fresh, "new producer epoch resets identity");

    std::atomic<bool> held = false;
    std::atomic<bool> release = false;
    std::thread holder([&] {
      WaitForSingleObject(writer.mutex, INFINITE);
      held.store(true);
      while (!release.load()) Sleep(1);
      ReleaseMutex(writer.mutex);
    });
    while (!held.load()) Sleep(1);
    const auto start = GetTickCount64();
    const auto busy = reader.read(out, 10);
    const auto duration = GetTickCount64() - start;
    release.store(true); holder.join();
    check(busy == quest3d::read_result::busy && duration < 250, "bounded lock wait");

    std::thread abandoned([&] { WaitForSingleObject(writer.mutex, INFINITE); });
    abandoned.join();
    check(reader.read(out, 0) == quest3d::read_result::invalid, "reject abandoned write");
    check(reader.read(out, 0) == quest3d::read_result::unchanged, "do not reaccept abandoned identity");
    h.frame_id++; writer.publish(h, 0x41);
    check(reader.read(out, 0) == quest3d::read_result::fresh, "recover after complete fresh publication");
    reader.close();
    check(reader.read(out, 0) == quest3d::read_result::fresh, "explicit reconnect");

    std::cout << "PASS: v2 layout, bounds, mapping recovery, physical ROI/SBS/padding coordinates, input permission and lifecycle guard\n";
    return 0;
  } catch (const std::exception &error) {
    std::cerr << "FAIL: " << error.what() << '\n';
    return 1;
  }
}
