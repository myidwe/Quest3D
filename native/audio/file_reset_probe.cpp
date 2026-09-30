/** @file native/audio/file_reset_probe.cpp
 * @brief Actual private PCM/Opus/gate teardown fixture, not a Sunshine session or Quest receipt.
 */
#include "src/platform/windows/quest3d_audio_reader.h"
#include "src/quest3d_audio_packet.h"
#include <opus/opus_multistream.h>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <iostream>
#include <thread>

namespace {
  /** @brief Shared barriers belong only to threads created by this inert fixture. */
  struct barriers {
    std::mutex mutex, output;
    std::condition_variable cv;
    bool encoded = false, release_send = false, retire = false, video_exit = false;
    std::atomic<bool> failed {false}, local_flushed {false}, audio_retired {false};
    std::atomic<unsigned> packets {0}, late_sends {0};
    const std::chrono::steady_clock::time_point deadline = std::chrono::steady_clock::now() + std::chrono::seconds(8);

    /** @brief Emit a bounded JSON event without interleaving threads. */
    void emit(const std::string &event) {
      std::lock_guard lock(output);
      std::cout << event << '\n' << std::flush;
    }
    /** @brief Wait for an actual test barrier; timeout is failure, never completion. */
    bool wait(bool &value) {
      std::unique_lock lock(mutex);
      if (!cv.wait_until(lock, deadline, [&] { return value; })) {
        failed = true;
        return false;
      }
      return true;
    }
    /** @brief Release only the explicitly requested fixture barrier. */
    void set(bool &value) {
      std::lock_guard lock(mutex);
      value = true;
      cv.notify_all();
    }
  };

  /** @brief Convert QPC to the producer's perf_counter nanosecond domain. */
  std::uint64_t now_ns() {
    LARGE_INTEGER c, f;
    QueryPerformanceCounter(&c); QueryPerformanceFrequency(&f);
    return static_cast<std::uint64_t>(c.QuadPart / f.QuadPart) * 1000000000ULL +
      static_cast<std::uint64_t>(c.QuadPart % f.QuadPart) * 1000000000ULL / f.QuadPart;
  }
}

/** @brief Exercise private actual reader retirement; stdin controls test barriers only. */
int main(int argc, char **argv) {
  if (argc != 2) return 2;
  const std::string channel = argv[1];
  barriers b;
  std::atomic<std::shared_ptr<quest3d::pcm::packet_scope>> scope {std::make_shared<quest3d::pcm::packet_scope>()};
  std::thread video([&] { b.wait(b.retire); b.set(b.video_exit); });  // No video capture/encode.
  std::thread sender([&] {
    if (!b.wait(b.encoded)) return;
    const auto sending_scope = scope.load();
    std::lock_guard gate(sending_scope->mutex);
    if (!sending_scope->active) { b.failed = true; return; }
    b.emit("{\"event\":\"gate_held\",\"network_send\":false}");
    b.wait(b.release_send);  // Holds the actual production scope mutex, no UDP.
  });
  std::thread audio([&] {
    {
      quest3d::pcm::reader source;
      if (!source.open(channel)) { b.failed = true; b.emit("{\"event\":\"open_failed\"}"); return; }
      b.emit("{\"event\":\"reader_open\",\"pid\":" + std::to_string(GetCurrentProcessId()) +
             ",\"creation_filetime\":\"" + std::to_string(quest3d::pcm::creation_time(GetCurrentProcess())) + "\"}");
      unsigned char mapping[] = {0, 1};
      int error = OPUS_OK;
      auto *encoder = opus_multistream_encoder_create(48000, 2, 1, 1, mapping, OPUS_APPLICATION_RESTRICTED_LOWDELAY, &error);
      if (!encoder || error != OPUS_OK) { b.failed = true; return; }
      const auto stop = [&] { scope.load()->stop(); };
      const auto encode = [&](const std::vector<float> &pcm) {
        std::array<unsigned char, 1400> bytes {};
        if (opus_multistream_encode_float(encoder, pcm.data(), 480, bytes.data(), bytes.size()) <= 0) return false;
        ++b.packets;
        b.set(b.encoded);
        return true;
      };
      while (std::chrono::steady_clock::now() < b.deadline) {
        const auto result = source.step(now_ns(), 480, encode, stop);
        if (result == quest3d::pcm::result::scope_changed) {
          if (b.packets) { b.failed = true; break; }
          scope.store(std::make_shared<quest3d::pcm::packet_scope>());
          opus_multistream_encoder_ctl(encoder, OPUS_RESET_STATE);
        }
        if (result == quest3d::pcm::result::failed || result == quest3d::pcm::result::closed) { b.failed = true; break; }
        if (result == quest3d::pcm::result::flushed) {
          b.local_flushed = true;
          b.emit("{\"event\":\"local_flush\",\"reader_retired\":false,\"worker_joined\":false,\"quest_queue_flushed\":false}");
          b.wait(b.retire);
          break;
        }
        if (result != quest3d::pcm::result::encoded) std::this_thread::sleep_for(std::chrono::milliseconds(1));
      }
      if (!b.local_flushed) b.failed = true;
      source.stop(stop);
      opus_multistream_encoder_destroy(encoder);
    }  // Actual reader destructor runs on its mutex-owning audio thread.
    b.audio_retired = true;
  });
  std::string command;
  while (std::getline(std::cin, command)) {
    if (command == "release_send") b.set(b.release_send);
    else if (command == "retire" && b.local_flushed) { b.set(b.retire); break; }
    else { b.failed = true; break; }
  }
  if (!b.local_flushed) b.failed = true;
  // Error cleanup is reported as failure; release only owned fixture barriers.
  b.set(b.release_send); b.set(b.retire); b.set(b.encoded);
  audio.join(); video.join(); sender.join();
  bool gate_closed;
  { const auto final_scope = scope.load(); std::lock_guard gate(final_scope->mutex); gate_closed = !final_scope->active; if (final_scope->active) ++b.late_sends; }
  const std::wstring name = L"Local\\Quest3D.Audio.v1." + std::wstring(channel.begin(), channel.end()) + L".Consumer";
  SetLastError(0);
  const HANDLE remaining = OpenMutexW(SYNCHRONIZE, FALSE, name.c_str());
  const bool ownership_object_gone = !remaining && GetLastError() == ERROR_FILE_NOT_FOUND;
  if (remaining) CloseHandle(remaining);
  // Both below are local fixture facts, never actual host session/Quest teardown.
  const bool success = !b.failed && b.local_flushed && b.audio_retired && b.video_exit && gate_closed && ownership_object_gone && b.packets > 0 && !b.late_sends;
  b.emit("{\"event\":\"fixture_retired\",\"scope\":\"diagnostic_only\",\"passed\":" + std::string(success ? "true" : "false") +
         ",\"audio_worker_joined\":true,\"video_fixture_joined\":true,\"reader_retired\":" + (b.audio_retired ? "true" : "false") +
         ",\"consumer_mutex_gone\":" + (ownership_object_gone ? "true" : "false") + ",\"packet_gate_closed\":" + (gate_closed ? "true" : "false") +
         ",\"encoded_packets\":" + std::to_string(b.packets) + ",\"actual_host_session_closed\":false,\"quest_queue_flushed\":false}");
  // Keep the child alive so the parent can prove reader retirement independently of process exit.
  if (!std::getline(std::cin, command) || command != "exit") return 6;
  return success ? 0 : 5;
}
