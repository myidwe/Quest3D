/** @file native/audio/pcm_bridge_probe.cpp
 * @brief Real private IPC and Opus encode/decode, without any audio endpoint or network.
 */
#include "src/platform/windows/quest3d_audio_reader.h"
#include <opus/opus_multistream.h>
#include <chrono>
#include <fstream>
#include <iostream>
#include <thread>

/** @brief The same QPC nanosecond domain used by Python perf_counter_ns on Windows. */
std::uint64_t now_ns() {
  LARGE_INTEGER counter, frequency;
  QueryPerformanceCounter(&counter); QueryPerformanceFrequency(&frequency);
  return static_cast<std::uint64_t>(counter.QuadPart / frequency.QuadPart) * 1000000000ULL +
    static_cast<std::uint64_t>(counter.QuadPart % frequency.QuadPart) * 1000000000ULL / frequency.QuadPart;
}

/** @brief Run a finite inert consumer; only a supplied test channel can be opened. */
int main(int argc, char **argv) {
  if (argc != 5) return 2;
  const unsigned duration_ms = std::stoul(argv[2]), frame_samples = std::stoul(argv[3]);
  if (!duration_ms || duration_ms > 10000 || (frame_samples != 240 && frame_samples != 480)) return 2;
  quest3d::pcm::reader source;
  if (!source.open(argv[1])) {
    std::cout << "{\"opened\":false}\n" << std::flush;
    if (std::getenv("QUEST3D_PCM_PROBE_HOLD_FAILED")) std::this_thread::sleep_for(std::chrono::milliseconds(500));
    return 3;
  }
  std::ofstream output(argv[4], std::ios::binary);
  if (!output) return 4;
  unsigned char mapping[] = {0,1};
  OpusMSEncoder *encoder = nullptr; OpusMSDecoder *decoder = nullptr;
  unsigned packets = 0, failed_steps = 0, flushes = 0, scopes = 0;
  const auto reset = [&] {
    if (encoder) opus_multistream_encoder_destroy(encoder);
    if (decoder) opus_multistream_decoder_destroy(decoder);
    encoder = opus_multistream_encoder_create(48000, 2, 1, 1, mapping, OPUS_APPLICATION_RESTRICTED_LOWDELAY, nullptr);
    decoder = opus_multistream_decoder_create(48000, 2, 1, 1, mapping, nullptr);
    opus_multistream_encoder_ctl(encoder, OPUS_SET_BITRATE(512000));
    opus_multistream_encoder_ctl(encoder, OPUS_SET_VBR(0));
  };
  const auto stop = [] {};
  const auto encode = [&](const std::vector<float> &pcm) {
    std::array<unsigned char, 1400> bytes;
    std::vector<float> decoded(frame_samples * 2);
    const auto size = opus_multistream_encode_float(encoder, pcm.data(), frame_samples, bytes.data(), bytes.size());
    if (size <= 0 || opus_multistream_decode_float(decoder, bytes.data(), size, decoded.data(), frame_samples, 0) != static_cast<int>(frame_samples)) return false;
    output.write(reinterpret_cast<const char *>(decoded.data()), decoded.size() * sizeof(float));
    ++packets;
    return output.good();
  };
  const auto until = std::chrono::steady_clock::now() + std::chrono::milliseconds(duration_ms);
  while (std::chrono::steady_clock::now() < until) {
    const auto result = source.step(now_ns(), frame_samples, encode, stop);
    if (result == quest3d::pcm::result::scope_changed) { reset(); ++scopes; }
    if (result == quest3d::pcm::result::flushed) ++flushes;
    if (result == quest3d::pcm::result::failed) ++failed_steps;
    if (result == quest3d::pcm::result::closed) break;
    if (result != quest3d::pcm::result::encoded) std::this_thread::sleep_for(std::chrono::milliseconds(1));
  }
  source.stop(stop);
  if (encoder) opus_multistream_encoder_destroy(encoder);
  if (decoder) opus_multistream_decoder_destroy(decoder);
  std::cout << "{\"opened\":true,\"packets\":" << packets << ",\"failed_steps\":" << failed_steps
    << ",\"flushes\":" << flushes << ",\"scopes\":" << scopes << ",\"audio_device_opened\":false}\n";
  return failed_steps ? 5 : 0;
}
