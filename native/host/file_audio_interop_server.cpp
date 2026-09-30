/**
 * @file native/host/file_audio_interop_server.cpp
 * @brief Private command-driven actual Sunshine TLS server for native-client interoperability.
 */
#include <atomic>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <future>
#include <iostream>
#include <src/config.h>
#include <src/nvhttp.h>
#include <src/quest3d_file_audio_stream.h>
#include <thread>

using namespace std::chrono_literals;
namespace fa = quest3d::file_audio;

/** @brief Emit one command acknowledgement without interpreting it as remote consumption. */
static void emit(const nlohmann::json &value) {
  std::cout << value.dump() << std::endl;
}

/** @brief Read independently generated actual-file bytes. */
static std::string read_file(const std::filesystem::path &path) {
  std::ifstream stream(path, std::ios::binary);
  return {std::istreambuf_iterator<char>(stream), {}};
}

/** @brief Report only the actual application's bounded write state. */
static nlohmann::json observation(const std::shared_ptr<fa::channel> &channel) {
  if (!channel) {
    return {{"published", false}};
  }
  const auto state = channel->observe();
  return {{"published", true}, {"attached", state.attached}, {"pending", state.write_pending}, {"drained", state.drained}, {"eof_sent", state.eof_sent}, {"reason", state.reason}, {"accepted", state.accepted_records}, {"sent", state.sent_records}};
}

/** @brief Own a private listener and explicit fixture lease; never start or replace live Sunshine. */
int main(int argc, char **argv) {
  if (argc != 3) {
    return 2;
  }
  const std::filesystem::path directory = argv[1];
  std::filesystem::create_directories(directory);
  config::sunshine.flags[config::flag::FRESH_STATE] = true;
  nvhttp::test_support::reset_client_state();
  const auto server_credentials = crypto::gen_creds("interop-server", 2048);
  const auto client_credentials = crypto::gen_creds("interop-client", 2048);
  const auto wrong_credentials = crypto::gen_creds("interop-unpaired", 2048);
  for (const auto &[name, value] : std::vector<std::pair<std::string, std::string>> {
         {"server.pem", server_credentials.x509},
         {"server.key", server_credentials.pkey},
         {"client.pem", client_credentials.x509},
         {"client.key", client_credentials.pkey},
         {"wrong.pem", wrong_credentials.x509},
         {"wrong.key", wrong_credentials.pkey}
       }) {
    std::ofstream(directory / name, std::ios::binary) << value;
  }
  const auto uuid = nvhttp::test_support::add_client("interop-client", client_credentials.x509, true);
  auto broker = std::make_shared<fa::broker>();
  auto server = nvhttp::test_support::file_audio_server((directory / "server.pem").string(), (directory / "server.key").string(), broker);
  // An existing WSL virtual-host interface, selected by the owning runner; no firewall edits.
  server->config.address = argv[2];
  std::promise<unsigned short> ready;
  auto future = ready.get_future();
  std::thread worker([&] {
    server->start([&](unsigned short port) {
      ready.set_value(port);
    });
  });
  std::shared_ptr<fa::channel> channel;
  std::vector<std::vector<std::uint8_t>> records;
  bool success = true;
  try {
    if (future.wait_for(4s) != std::future_status::ready) {
      throw std::runtime_error("listener_start_failed");
    }
    emit({{"ready", true}, {"port", future.get()}});
    std::string line;
    while (std::getline(std::cin, line)) {
      const auto request = nlohmann::json::parse(line);
      const auto op = request.at("op").get<std::string>();
      if (op == "stop") {
        break;
      }
      if (op == "publish") {
        if (channel && !channel->observe().drained) {
          throw std::runtime_error("previous_channel_not_drained");
        }
        const auto name = request.at("file").get<std::string>();
        if (name != "first" && name != "seek") {
          throw std::runtime_error("unknown_fixture_file");
        }
        const auto bytes = read_file(std::filesystem::path(FILE_AUDIO_VECTOR_DIR) / (name + ".wire"));
        records.clear();
        for (std::size_t offset = 0; offset < bytes.size();) {
          if (bytes.size() - offset < fa::header_bytes) {
            throw std::runtime_error("bad_vector");
          }
          const auto size = fa::header_bytes + fa::read_le(reinterpret_cast<const std::uint8_t *>(bytes.data() + offset + 12), 4);
          if (size > bytes.size() - offset) {
            throw std::runtime_error("short_vector");
          }
          records.emplace_back(bytes.begin() + offset, bytes.begin() + offset + size);
          offset += size;
        }
        fa::scope scope;
        scope.file_session.fill(0x11);
        scope.transport.fill(0x22);
        scope.channel.fill(0x33);
        scope.epoch = name == "first" ? 7 : 8;
        scope.generation = name == "first" ? 9 : 10;
        // Explicit fixture authorization; this is not the production file coordinator.
        channel = nvhttp::test_support::publish_test_file_audio_binding(*broker, scope, client_credentials.x509, [](const auto &dispatch) {
          dispatch();
          return true;
        });
        if (!channel) {
          throw std::runtime_error("publication_refused");
        }
      } else if (op == "push") {
        const auto first = request.at("first").get<std::size_t>();
        const auto end = request.at("end").get<std::size_t>();
        if (!channel || first > end || end > records.size()) {
          throw std::runtime_error("invalid_fixture_range");
        }
        for (auto index = first; index < end; ++index) {
          const auto until = std::chrono::steady_clock::now() + 4s;
          auto result = channel->try_enqueue(records[index]);
          while (result == fa::enqueue_result::full && std::chrono::steady_clock::now() < until) {
            std::this_thread::sleep_for(1ms);
            result = channel->try_enqueue(records[index]);
          }
          if (result != fa::enqueue_result::accepted) {
            throw std::runtime_error("record_handoff_failed");
          }
        }
      } else if (op == "revoke") {
        if (channel) {
          channel->revoke("interop_revoke");
        }
      } else if (op != "observe") {
        throw std::runtime_error("unknown_fixture_command");
      }
      emit(observation(channel));
    }
  } catch (const std::exception &error) {
    emit({{"error", error.what()}});
    success = false;
  }
  broker->revoke();
  const auto until = std::chrono::steady_clock::now() + 4s;
  while (channel && !channel->observe().drained && std::chrono::steady_clock::now() < until) {
    std::this_thread::sleep_for(1ms);
  }
  if (channel && !channel->observe().drained) {
    success = false;
  }
  server->stop();
  worker.join();
  emit({{"stopped", true}, {"callbacks_drained", !channel || channel->observe().drained}, {"success", success}});
  return success ? 0 : 1;
}
