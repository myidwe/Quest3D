/**
 * @file native/host/file_audio_ipc_reader_cli.cpp
 * @brief Test-only interactive native IPC consumer; file writes are bounded sink handoffs, never playback ACKs.
 */
#include "src/platform/windows/quest3d_file_audio_ipc_reader.h"

#include <charconv>
#include <fstream>
#include <iostream>

namespace ipc = quest3d::file_audio::ipc;

/** @brief Parse exactly one lowercase, fixed-size identifier. */
bool parse_id(std::string_view text, std::array<std::uint8_t, 16> &id) {
  if (text.size() != 32) {
    return false;
  }
  for (std::size_t i = 0; i < id.size(); ++i) {
    auto digit = [](char c) {
      return c >= '0' && c <= '9' ? c - '0' : c >= 'a' && c <= 'f' ? c - 'a' + 10 :
                                                                     -1;
    };
    const int high = digit(text[i * 2]), low = digit(text[i * 2 + 1]);
    if (high < 0 || low < 0) {
      return false;
    }
    id[i] = static_cast<std::uint8_t>((high << 4) | low);
  }
  return true;
}

/** @brief Parse a full uint64 decimal without partial conversion or a sign. */
bool parse_number(std::string_view text, std::uint64_t &value) {
  const auto result = std::from_chars(text.data(), text.data() + text.size(), value);
  return result.ec == std::errc {} && result.ptr == text.data() + text.size();
}

/** @brief Emit flushed machine-readable actual reader facts for cross-process barriers. */
void report(std::string_view command, bool ok, const ipc::reader &reader, std::optional<std::uint64_t> offered = {}) {
  const auto value = reader.snapshot();
  const auto &h = value.last_verified;
  std::cout << "{\"command\":\"" << command << "\",\"ok\":" << (ok ? "true" : "false")
            << ",\"error\":" << int(value.error) << ",\"windows_error\":" << value.windows_error
            << ",\"head\":" << h.head << ",\"tail\":" << h.tail << ",\"handed_frames\":" << h.handed_frames
            << ",\"producer_pid\":" << h.producer_pid << ",\"producer_creation\":" << h.producer_creation
            << ",\"consumer_pid\":" << h.consumer_pid << ",\"consumer_creation\":" << h.consumer_creation
            << ",\"accepting\":" << (value.accepting ? "true" : "false")
            << ",\"pending\":" << (value.offer_pending ? "true" : "false")
            << ",\"eof_handed\":" << (value.eof_handed ? "true" : "false")
            << ",\"stopped\":" << (value.stopped ? "true" : "false")
            << ",\"resources_released\":" << (value.resources_released ? "true" : "false")
            << ",\"offered_sequence\":";
  if (offered) {
    std::cout << *offered;
  } else {
    std::cout << "null";
  }
  std::cout << "}" << std::endl;
}

/** @brief Remain alive after OPEN until explicit test commands release the actual consumer process. */
int main(int argc, char **argv) {
  quest3d::file_audio::scope expected;
  if (argc != 7 || !parse_id(argv[1], expected.file_session) || !parse_id(argv[2], expected.transport) ||
      !parse_id(argv[3], expected.channel) || !parse_number(argv[4], expected.epoch) || !parse_number(argv[5], expected.generation)) {
    return 2;
  }
  std::ofstream output(argv[6], std::ios::binary | std::ios::trunc);
  if (!output) {
    return 3;
  }
  ipc::reader reader;
  const auto opened = reader.open(expected);
  report("open", opened, reader);
  if (!opened) {
    return 4;
  }
  std::optional<ipc::offer> offered;
  std::optional<ipc::offer> accepted;
  for (std::string command; std::getline(std::cin, command);) {
    bool ok = false;
    if (command == "peek") {
      offered = reader.peek_record();
      ok = offered.has_value();
    } else if (command == "accept" && offered) {
      const auto bytes = offered->bytes();
      output.write(reinterpret_cast<const char *>(bytes.data()), static_cast<std::streamsize>(bytes.size()));
      output.flush();
      if (output) {
        ok = reader.commit_handoff(*offered);
        accepted = offered;
      }
      if (!ok) {
        reader.stop();  // Accepted-but-uncommitted cannot be replayed as a new handoff.
      }
      offered.reset();
    } else if (command == "replay" && accepted) {
      ok = reader.commit_handoff(*accepted);
    } else if (command == "snapshot") {
      ok = true;
    } else if (command == "stop") {
      ok = reader.stop();
    } else if (command == "retire") {
      ok = reader.retire().has_value();
    } else if (command == "quit") {
      report(command, true, reader);
      return 0;
    }
    report(command, ok, reader, offered ? std::optional(offered->sequence()) : std::nullopt);
  }
  return 0;
}
