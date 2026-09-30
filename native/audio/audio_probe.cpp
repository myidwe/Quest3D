/** @file audio_probe.cpp
 * @brief Read-only Core Audio endpoint and PCM timing probe; never renders sound.
 */
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <audioclient.h>
#include <endpointvolume.h>
#include <mmdeviceapi.h>
#include <propsys.h>
#include <propkeydef.h>
#include <functiondiscoverykeys_devpkey.h>
#include <wrl/client.h>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>
#include <deque>
#include "../../third_party/sunshine/src/audio_delay.h"

using Microsoft::WRL::ComPtr;

/** @brief Raise a readable error when a COM operation fails. */
void check(HRESULT value, const char *operation) {
  if (FAILED(value)) {
    std::ostringstream message;
    message << operation << " HRESULT=0x" << std::hex << static_cast<unsigned long>(value);
    throw std::runtime_error(message.str());
  }
}

/** @brief Convert Windows text to JSON-safe UTF-8 including endpoint names. */
std::string json_string(const std::wstring &value) {
  const auto size = WideCharToMultiByte(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), nullptr, 0, nullptr, nullptr);
  std::string utf8(size, '\0');
  WideCharToMultiByte(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), utf8.data(), size, nullptr, nullptr);
  std::ostringstream output;
  output << '"';
  for (const auto character : utf8) {
    if (character == '"' || character == '\\') {
      output << '\\' << character;
    } else if (static_cast<unsigned char>(character) < 32) {
      output << "\\u" << std::hex << std::setw(4) << std::setfill('0') << static_cast<int>(static_cast<unsigned char>(character));
    } else {
      output << character;
    }
  }
  output << '"';
  return output.str();
}

/** @brief Return the endpoint's stable Windows ID without changing its state. */
std::wstring endpoint_id(IMMDevice *device) {
  LPWSTR raw = nullptr;
  check(device->GetId(&raw), "GetId");
  std::wstring id(raw);
  CoTaskMemFree(raw);
  return id;
}

/** @brief Serialize an endpoint's current name, volume and mute state. */
std::string inspect_endpoint(IMMDevice *device) {
  ComPtr<IPropertyStore> properties;
  check(device->OpenPropertyStore(STGM_READ, &properties), "OpenPropertyStore");
  PROPVARIANT name;
  PropVariantInit(&name);
  check(properties->GetValue(PKEY_Device_FriendlyName, &name), "FriendlyName");
  std::wstring friendly = name.vt == VT_LPWSTR ? name.pwszVal : L"";
  PropVariantClear(&name);
  ComPtr<IAudioEndpointVolume> volume;
  check(device->Activate(__uuidof(IAudioEndpointVolume), CLSCTX_ALL, nullptr, &volume), "Activate volume");
  BOOL muted = FALSE;
  float scalar = 0;
  check(volume->GetMute(&muted), "GetMute");
  check(volume->GetMasterVolumeLevelScalar(&scalar), "GetMasterVolumeLevelScalar");
  std::ostringstream output;
  output << "{\"id\":" << json_string(endpoint_id(device)) << ",\"name\":" << json_string(friendly)
         << ",\"muted\":" << (muted ? "true" : "false") << ",\"volume_scalar\":" << scalar << '}';
  return output.str();
}

/** @brief Inspect each role separately so restoration can preserve differing defaults. */
std::string inspect_defaults(IMMDeviceEnumerator *enumerator) {
  const char *names[] = {"console", "multimedia", "communications"};
  std::ostringstream output;
  output << '{';
  for (int index = 0; index < 3; ++index) {
    if (index) output << ',';
    output << '"' << names[index] << "\":";
    ComPtr<IMMDevice> device;
    const auto status = enumerator->GetDefaultAudioEndpoint(eRender, static_cast<ERole>(index), &device);
    if (FAILED(status)) output << "null";
    else output << inspect_endpoint(device.Get());
  }
  output << '}';
  return output.str();
}

/** @brief Capture only aggregate PCM/timing statistics; no PCM is saved or rendered. */
std::string capture_statistics(IMMDevice *device, int seconds, unsigned delay_ms) {
  ComPtr<IAudioClient> client;
  check(device->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr, &client), "Activate audio client");
  WAVEFORMATEX format{};
  format.wFormatTag = WAVE_FORMAT_IEEE_FLOAT;
  format.nChannels = 2;
  format.nSamplesPerSec = 48000;
  format.wBitsPerSample = 32;
  format.nBlockAlign = 8;
  format.nAvgBytesPerSec = 384000;
  check(client->Initialize(AUDCLNT_SHAREMODE_SHARED,
      AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK |
      AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,
      1000000, 0, &format, nullptr), "Initialize 48kHz float loopback");
  HANDLE event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
  if (!event) throw std::runtime_error("CreateEvent failed");
  struct close_event { HANDLE value; ~close_event() { CloseHandle(value); } } event_guard{event};
  check(client->SetEventHandle(event), "SetEventHandle");
  ComPtr<IAudioCaptureClient> capture;
  check(client->GetService(__uuidof(IAudioCaptureClient), &capture), "GetService capture");
  REFERENCE_TIME period = 0;
  check(client->GetDevicePeriod(&period, nullptr), "GetDevicePeriod");
  LARGE_INTEGER frequency;
  QueryPerformanceFrequency(&frequency);
  std::uint64_t frames = 0, packets = 0, silent_frames = 0, discontinuities = 0, timestamp_errors = 0;
  std::uint64_t first_qpc = 0, last_qpc = 0, nonfinite = 0;
  long double energy = 0;
  double peak = 0;
  std::vector<double> ages;
  audio::pcm_delay_t delay {48000, 2};
  delay.configure(delay_ms);
  std::deque<float> reference(static_cast<std::size_t>(48000) * delay_ms / 1000 * 2, 0.0f);
  std::uint64_t delay_checked_samples = 0, delay_mismatches = 0;
  check(client->Start(), "Start capture");
  struct stop_client { IAudioClient *value; ~stop_client() { value->Stop(); } } client_guard{client.Get()};
  const auto started = std::chrono::steady_clock::now();
  while (std::chrono::steady_clock::now() - started < std::chrono::seconds(seconds)) {
    const auto result = WaitForSingleObject(event, 50);
    if (result == WAIT_TIMEOUT) continue;
    if (result != WAIT_OBJECT_0) throw std::runtime_error("Audio event wait failed");
    UINT32 available = 0;
    check(capture->GetNextPacketSize(&available), "GetNextPacketSize");
    while (available) {
      BYTE *raw = nullptr;
      UINT32 count = 0;
      DWORD flags = 0;
      UINT64 position = 0, qpc_100ns = 0;
      check(capture->GetBuffer(&raw, &count, &flags, &position, &qpc_100ns), "GetBuffer");
      LARGE_INTEGER now;
      QueryPerformanceCounter(&now);
      const auto now_100ns = static_cast<long double>(now.QuadPart) * 10000000 / frequency.QuadPart;
      if (flags & AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR) ++timestamp_errors;
      else {
        ages.push_back(static_cast<double>((now_100ns - qpc_100ns) / 10000));
        if (!first_qpc) first_qpc = qpc_100ns;
        last_qpc = qpc_100ns;
      }
      if (flags & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY) ++discontinuities;
      std::vector<float> block(static_cast<std::size_t>(count) * 2, 0.0f);
      if (flags & AUDCLNT_BUFFERFLAGS_SILENT) silent_frames += count;
      else {
        const auto samples = reinterpret_cast<const float *>(raw);
        std::copy_n(samples, block.size(), block.begin());
        for (std::uint64_t index = 0; index < static_cast<std::uint64_t>(count) * 2; ++index) {
          const auto sample = samples[index];
          if (!std::isfinite(sample)) { ++nonfinite; continue; }
          energy += static_cast<long double>(sample) * sample;
          peak = std::max(peak, std::abs(static_cast<double>(sample)));
        }
      }
      reference.insert(reference.end(), block.begin(), block.end());
      delay.process(block);
      for (const auto value : block) {
        const auto expected = reference.front();
        reference.pop_front();
        if (std::isfinite(value) && std::isfinite(expected)) {
          ++delay_checked_samples;
          if (value != expected) ++delay_mismatches;
        }
      }
      frames += count;
      ++packets;
      check(capture->ReleaseBuffer(count), "ReleaseBuffer");
      check(capture->GetNextPacketSize(&available), "GetNextPacketSize");
    }
  }
  std::sort(ages.begin(), ages.end());
  std::ostringstream output;
  output << "{\"endpoint_id\":" << json_string(endpoint_id(device))
         << ",\"sample_rate\":48000,\"channels\":2,\"float_bits\":32"
         << ",\"duration_seconds\":" << std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count()
         << ",\"device_period_ms\":" << period / 10000.0 << ",\"frames\":" << frames
         << ",\"packets\":" << packets << ",\"silent_frames\":" << silent_frames
         << ",\"discontinuities\":" << discontinuities << ",\"timestamp_errors\":" << timestamp_errors
         << ",\"nonfinite_samples\":" << nonfinite << ",\"rms\":" << (frames ? std::sqrt(energy / (frames * 2)) : 0)
         << ",\"peak\":" << peak << ",\"first_qpc_100ns\":" << first_qpc << ",\"last_qpc_100ns\":" << last_qpc
         << ",\"delay_ms\":" << delay_ms << ",\"delay_checked_samples\":" << delay_checked_samples
         << ",\"delay_sample_mismatches\":" << delay_mismatches
         << ",\"delay_sample_offset_verified\":" << (delay_checked_samples > static_cast<std::uint64_t>(48000) * delay_ms / 1000 * 2 && delay_mismatches == 0 ? "true" : "false")
         << ",\"packet_first_sample_age_ms_p50\":" << (ages.empty() ? 0 : ages[ages.size() / 2])
         << ",\"packet_first_sample_age_ms_p95\":" << (ages.empty() ? 0 : ages[(ages.size() - 1) * 95 / 100]) << '}';
  return output.str();
}

/** @brief Main read-only probe; optional --seconds 1..10 captures aggregate data. */
int wmain(int argc, wchar_t **argv) {
  try {
    int seconds = 0;
    int delay_ms = 0;
    std::wstring selected;
    for (int index = 1; index < argc; ++index) {
      const std::wstring argument = argv[index];
      if (argument == L"--seconds" && index + 1 < argc) seconds = std::stoi(argv[++index]);
      else if (argument == L"--delay-ms" && index + 1 < argc) delay_ms = std::stoi(argv[++index]);
      else if (argument == L"--endpoint" && index + 1 < argc) selected = argv[++index];
      else throw std::runtime_error("Use --seconds 0..10, --delay-ms 0..500 and optional --endpoint ID");
    }
    if (seconds < 0 || seconds > 10) throw std::runtime_error("seconds must be 0..10");
    if (delay_ms < 0 || delay_ms > 500) throw std::runtime_error("delay-ms must be 0..500");
    check(CoInitializeEx(nullptr, COINIT_MULTITHREADED), "CoInitializeEx");
    struct uninitialize { ~uninitialize() { CoUninitialize(); } } com_guard;
    ComPtr<IMMDeviceEnumerator> enumerator;
    check(CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL, IID_PPV_ARGS(&enumerator)), "Create enumerator");
    const auto before = inspect_defaults(enumerator.Get());
    ComPtr<IMMDeviceCollection> devices;
    check(enumerator->EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE, &devices), "EnumAudioEndpoints");
    UINT count = 0;
    check(devices->GetCount(&count), "Endpoint count");
    std::ostringstream inventory;
    inventory << '[';
    for (UINT index = 0; index < count; ++index) {
      ComPtr<IMMDevice> device;
      check(devices->Item(index, &device), "Endpoint item");
      if (index) inventory << ',';
      inventory << inspect_endpoint(device.Get());
    }
    inventory << ']';
    std::string capture = "null";
    if (seconds) {
      ComPtr<IMMDevice> device;
      if (selected.empty()) check(enumerator->GetDefaultAudioEndpoint(eRender, eConsole, &device), "Default capture endpoint");
      else check(enumerator->GetDevice(selected.c_str(), &device), "Selected capture endpoint");
      capture = capture_statistics(device.Get(), seconds, static_cast<unsigned>(delay_ms));
    }
    const auto after = inspect_defaults(enumerator.Get());
    std::cout << "{\"read_only\":true,\"renders_audio\":false,\"saves_pcm\":false,\"defaults_before\":" << before
              << ",\"active_render_endpoints\":" << inventory.str() << ",\"capture\":" << capture
              << ",\"defaults_after\":" << after << ",\"defaults_unchanged\":" << (before == after ? "true" : "false") << "}\n";
    return 0;
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
