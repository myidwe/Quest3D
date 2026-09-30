/**
 * @file native/host/file_audio_stream_test.cpp
 * @brief Private actual HTTPS route tests; owner injection is not a product coordinator.
 */
#include <atomic>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <future>
#include <gtest/gtest.h>
#include <Simple-Web-Server/client_https.hpp>
#include <src/config.h>
#include <src/nvhttp.h>
#include <src/quest3d_file_audio_stream.h>
#include <thread>

using namespace std::chrono_literals;
namespace fa = quest3d::file_audio;

/** @brief Read only a task-owned fixture or generated wire vector. */
static std::string read_file(const std::filesystem::path &path) {
  std::ifstream stream(path, std::ios::binary);
  return {std::istreambuf_iterator<char>(stream), {}};
}

/** @brief Bounded wait used only to synchronize fixture observations. */
template<class Predicate>
static bool await(Predicate predicate) {
  const auto deadline = std::chrono::steady_clock::now() + 4s;
  while (std::chrono::steady_clock::now() < deadline) {
    if (predicate()) {
      return true;
    }
    std::this_thread::sleep_for(1ms);
  }
  return predicate();
}

/** @brief Use a private actual TLS server and generated paired credentials. */
class FileAudioHttps: public testing::Test {
protected:
  std::shared_ptr<fa::broker> broker = std::make_shared<fa::broker>();  ///< Private registry only.
  std::shared_ptr<SimpleWeb::ServerBase<nvhttp::SunshineHTTPS>> server;  ///< Actual backend under test.
  std::thread server_thread;  ///< Owned server executor.
  std::shared_ptr<fa::channel> channel;  ///< Actual producer handoff state.
  crypto::creds_t credentials, server_credentials, wrong_credentials;  ///< Generated test-only keys.
  std::string uuid;  ///< Exact paired fixture identity.
  std::filesystem::path directory;  ///< Private compile-selected output directory.
  unsigned short port = 0;  ///< OS-selected loopback port, never a product port.
  std::atomic_bool current_owner {true};  ///< Explicit injected launch/file lease.
  fa::scope scope;  ///< Root's immutable actual-file vector scope.
  std::vector<std::vector<std::uint8_t>> records;  ///< Root MediaAudioReader records.
  std::string wire;  ///< Expected complete response body.

  /** @brief Start the real server with fresh in-memory paired state. */
  void SetUp() override {
    directory = std::filesystem::path(SUNSHINE_TEST_BIN_DIR) / testing::UnitTest::GetInstance()->current_test_info()->name();
    std::filesystem::create_directories(directory);
    config::sunshine.flags[config::flag::FRESH_STATE] = true;
    nvhttp::test_support::reset_client_state();
    credentials = crypto::gen_creds("private-file-client", 2048);
    wrong_credentials = crypto::gen_creds("private-other-client", 2048);
    server_credentials = crypto::gen_creds("private-file-server", 2048);
    for (const auto &[name, value] : std::vector<std::pair<std::string, std::string>> {
           {"server.pem", server_credentials.x509},
           {"server.key", server_credentials.pkey},
           {"client.pem", credentials.x509},
           {"client.key", credentials.pkey},
           {"wrong.pem", wrong_credentials.x509},
           {"wrong.key", wrong_credentials.pkey}
         }) {
      std::ofstream(directory / name, std::ios::binary) << value;
    }
    uuid = nvhttp::test_support::add_client("file-fixture", credentials.x509, true);
    ASSERT_FALSE(uuid.empty());
    scope.file_session.fill(0x11);
    scope.transport.fill(0x22);
    scope.channel.fill(0x33);
    scope.epoch = 7;
    scope.generation = 9;
    load_wire("first");
    ASSERT_EQ(records.size(), 12);
    server = nvhttp::test_support::file_audio_server((directory / "server.pem").string(), (directory / "server.key").string(), broker);
    std::promise<unsigned short> ready;
    auto future = ready.get_future();
    server_thread = std::thread([this, &ready] {
      server->start([&ready](unsigned short bound) {
        ready.set_value(bound);
      });
    });
    ASSERT_EQ(future.wait_for(4s), std::future_status::ready);
    port = future.get();
  }

  /** @brief Read an independently encoded actual-file response into bounded record views. */
  void load_wire(const std::string &name) {
    wire = read_file(std::filesystem::path(FILE_AUDIO_VECTOR_DIR) / (name + ".wire"));
    records.clear();
    ASSERT_FALSE(wire.empty());
    for (std::size_t offset = 0; offset < wire.size();) {
      ASSERT_GE(wire.size() - offset, fa::header_bytes);
      const auto length = fa::header_bytes + fa::read_le(reinterpret_cast<const std::uint8_t *>(wire.data()) + offset + 12, 4);
      ASSERT_LE(length, wire.size() - offset);
      records.emplace_back(wire.begin() + offset, wire.begin() + offset + length);
      offset += length;
    }
  }

  /** @brief Release this broker before stopping its executor; never touch live Sunshine. */
  void TearDown() override {
    const auto started = std::chrono::steady_clock::now();
    broker->revoke();
    if (server) {
      server->stop();
    }
    if (server_thread.joinable()) {
      server_thread.join();
    }
    server.reset();
    nvhttp::test_support::reset_client_state();
    std::cout << "fixture server teardown ms=" << std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - started).count() << '\n';
  }

  /** @brief Publish an explicit fixture lease through the production paired-auth gate. */
  void publish() {
    channel = nvhttp::test_support::publish_test_file_audio_binding(*broker, scope, credentials.x509, [this](const auto &dispatch) {
      if (!current_owner.load()) {
        return false;
      }
      dispatch();
      return true;
    });
    ASSERT_TRUE(channel);
  }

  /** @brief Render the exact five canonical request fields. */
  std::string body() const {
    return nlohmann::json {{"file_session", "11111111111111111111111111111111"}, {"transport", "22222222222222222222222222222222"}, {"channel", "33333333333333333333333333333333"}, {"epoch", std::to_string(scope.epoch)}, {"generation", std::to_string(scope.generation)}}.dump();
  }

  /** @brief Exact-leaf OpenSSL fixture client that rejects unclean TLS truncation. */
  class tls_client: public SimpleWeb::Client<SimpleWeb::HTTPS> {
  public:
    /** @brief Keep client I/O on the calling fixture thread with a bounded deadline. */
    tls_client(const std::string &host, const std::string &cert, const std::string &key, const std::string &expected):
        SimpleWeb::Client<SimpleWeb::HTTPS>(host, false, cert, key) {
      context.set_verify_mode(boost::asio::ssl::verify_peer);
      context.set_verify_callback([expected](bool, boost::asio::ssl::verify_context &ctx) {
        auto certificate = crypto::x509(expected);
        return X509_STORE_CTX_get_error_depth(ctx.native_handle()) == 0 &&
               certificate && X509_cmp(certificate.get(), X509_STORE_CTX_get_current_cert(ctx.native_handle())) == 0;
      });
      config.timeout = 4;
      config.timeout_connect = 4;
      config.max_response_streambuf_size = 65536;
      io_service = std::make_shared<boost::asio::io_context>();
    }

  protected:
    /** @brief Require TLS close_notify; stream_truncated remains an actual error. */
    SimpleWeb::error_code clean_error_code(const SimpleWeb::error_code &error) override {
      return error == boost::asio::error::eof ? SimpleWeb::error_code() : error;
    }
  };

  /** @brief Return transport error, HTTP status, headers and exact received bytes. */
  struct reply {
    int code = 0;  ///< Actual OpenSSL/Asio error value; zero only after successful HTTP end.
    long status = 0;  ///< Actual HTTP status.
    std::string bytes, headers;  ///< Received response data.
  };

  /** @brief Use exact generated server-leaf verification and optional client credentials. */
  reply request(std::string request_body, int client = 1, tls_client *reuse = nullptr, bool first = false) {
    reply result;
    auto owned = reuse ? nullptr : make_client(client);
    auto &connection = reuse ? *reuse : *owned;
    connection.io_service->restart();
    connection.request(first ? "GET" : "POST", first ? "/fixture/first" : "/quest3d/v1/file-audio", request_body, {{"Content-Type", "application/json"}}, [&](auto response, const SimpleWeb::error_code &error) {
      result.code = error.value();
      if (response) {
        if (!response->status_code.empty()) {
          result.status = std::stol(response->status_code);
        }
        result.bytes += response->content.string();
        for (const auto &[key, value] : response->header) {
          result.headers += key + ": " + value + "\r\n";
        }
      }
      if (error) {
        std::cerr << "fixture TLS error: " << error.message() << '\n';
      }
      connection.io_service->stop();
    });
    connection.io_service->run();
    return result;
  }

  /** @brief Construct a fixture client without touching the Windows certificate store. */
  std::unique_ptr<tls_client> make_client(int client) {
    return std::make_unique<tls_client>("127.0.0.1:" + std::to_string(port), client ? (directory / (client == 2 ? "wrong.pem" : "client.pem")).string() : "", client ? (directory / (client == 2 ? "wrong.key" : "client.key")).string() : "", server_credentials.x509);
  }

  /** @brief Open actual mTLS and stop reading after the exact POST, exercising socket backpressure. */
  class stalled_peer {
  public:
    boost::asio::io_context io;  ///< Owned synchronous peer executor.
    boost::asio::ssl::context context {boost::asio::ssl::context::tls_client};  ///< Private generated credentials.
    boost::asio::ssl::stream<boost::asio::ip::tcp::socket> socket {io, context};  ///< Real TLS socket, never a fake sender.

    /** @brief Connect only to the private listener with exact-leaf verification. */
    stalled_peer(FileAudioHttps &fixture) {
      context.use_certificate_chain_file((fixture.directory / "client.pem").string());
      context.use_private_key_file((fixture.directory / "client.key").string(), boost::asio::ssl::context::pem);
      // The stream predates context configuration, so install its own client credentials.
      auto certificate = crypto::x509(fixture.credentials.x509);
      auto key = crypto::pkey(fixture.credentials.pkey);
      EXPECT_EQ(SSL_use_certificate(socket.native_handle(), certificate.get()), 1);
      EXPECT_EQ(SSL_use_PrivateKey(socket.native_handle(), key.get()), 1);
      socket.set_verify_mode(boost::asio::ssl::verify_peer);
      socket.set_verify_callback([expected = fixture.server_credentials.x509](bool, boost::asio::ssl::verify_context &ctx) {
        auto expected_leaf = crypto::x509(expected);
        return X509_STORE_CTX_get_error_depth(ctx.native_handle()) == 0 &&
               X509_cmp(expected_leaf.get(), X509_STORE_CTX_get_current_cert(ctx.native_handle())) == 0;
      });
      socket.next_layer().open(boost::asio::ip::tcp::v4());
      socket.next_layer().set_option(boost::asio::socket_base::receive_buffer_size(1024));
      socket.next_layer().connect({boost::asio::ip::address_v4::loopback(), fixture.port});
      socket.handshake(boost::asio::ssl::stream_base::client);
      const auto body = fixture.body();
      const auto request = "POST /quest3d/v1/file-audio HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: " +
                           std::to_string(body.size()) + "\r\n\r\n" + body;
      boost::asio::write(socket, boost::asio::buffer(request));
    }
  };

  /** @brief Fill actual TLS/socket buffers until one bounded record write stops making progress. */
  bool reach_stalled_write() {
    auto bytes = records.front();
    std::uint64_t sequence = 0, last_sent = 0;
    auto unchanged_since = std::chrono::steady_clock::now();
    const auto deadline = unchanged_since + 4s;
    while (std::chrono::steady_clock::now() < deadline) {
      for (std::size_t i = 0; i < 8; ++i) {
        bytes[80 + i] = static_cast<std::uint8_t>(sequence >> (i * 8));
      }
      const auto result = channel->try_enqueue(bytes);
      if (result == fa::enqueue_result::accepted) {
        ++sequence;
      } else if (result != fa::enqueue_result::full) {
        return false;
      }
      const auto state = channel->observe();
      if (state.sent_records != last_sent) {
        last_sent = state.sent_records;
        unchanged_since = std::chrono::steady_clock::now();
      }
      if (state.write_pending && state.queued_records == 3 && std::chrono::steady_clock::now() - unchanged_since > 200ms) {
        std::cout << "actual stalled send after accepted=" << state.accepted_records << " sent=" << state.sent_records << '\n';
        return true;
      }
      if (result == fa::enqueue_result::full) {
        std::this_thread::sleep_for(1ms);
      }
    }
    return false;
  }
};

TEST_F(FileAudioHttps, DefaultBindingUnavailableAndWrongCertificateDenied) {
  auto unavailable = request(body());
  EXPECT_EQ(unavailable.code, 0);
  EXPECT_EQ(unavailable.status, 503);
  EXPECT_NE(unavailable.bytes.find("file_binding_unavailable"), std::string::npos);
  const auto wrong = request(body(), 2);
  EXPECT_EQ(wrong.status, 403);
  const auto missing = request(body(), 0);
  EXPECT_NE(missing.code, 0);
  publish();
  ASSERT_FALSE(nvhttp::test_support::add_client("other-paired-owner", wrong_credentials.x509, true).empty());
  const auto other_owner = request(body(), 2);
  EXPECT_EQ(other_owner.status, 403);
  EXPECT_NE(other_owner.bytes.find("file_scope_owner_required"), std::string::npos);
}

TEST_F(FileAudioHttps, SecondRequestOnVerifiedConnectionDoesNotInheritIdentity) {
  publish();
  auto client = make_client(1);
  const auto first = request({}, 1, client.get(), true);
  EXPECT_EQ(first.status, 200);
  const auto second = request(body(), 1, client.get());
  EXPECT_EQ(second.status, 403);
  EXPECT_NE(second.bytes.find("paired_request_certificate_required"), std::string::npos);
  EXPECT_FALSE(channel->observe().attached);
}

TEST_F(FileAudioHttps, ActualMediaAudioReaderRecordsArriveIncrementallyAndEndNormally) {
  for (int pass = 0; pass < 2; ++pass) {
    if (pass) {
      scope.epoch = 8;
      scope.generation = 10;
      load_wire("seek");
      ASSERT_EQ(records.size(), 10);
    }
    publish();
    for (std::size_t i = 0; i < 4; ++i) {
      ASSERT_EQ(channel->try_enqueue(records[i]), fa::enqueue_result::accepted);
    }
    EXPECT_EQ(channel->try_enqueue(records[4]), fa::enqueue_result::full);
    EXPECT_EQ(channel->observe().accepted_records, 4);
    auto receiving = std::async(std::launch::async, [this] {
      return request(body());
    });
    for (std::size_t i = 4; i < records.size(); ++i) {
      fa::enqueue_result accepted = fa::enqueue_result::full;
      ASSERT_TRUE(await([&] {
        accepted = channel->try_enqueue(records[i]);
        return accepted != fa::enqueue_result::full;
      }));
      ASSERT_EQ(accepted, fa::enqueue_result::accepted);
    }
    const auto received = receiving.get();
    EXPECT_EQ(received.code, 0);
    EXPECT_EQ(received.status, 200);
    EXPECT_EQ(received.bytes, wire);
    EXPECT_NE(received.headers.find(fa::content_type), std::string::npos);
    EXPECT_EQ(received.headers.find("Content-Length"), std::string::npos);
    const auto state = channel->observe();
    EXPECT_TRUE(state.eof_sent && state.closed && state.drained);
    EXPECT_FALSE(state.write_pending);
    EXPECT_EQ(state.accepted_records, records.size());
    EXPECT_EQ(state.sent_records, records.size());
    EXPECT_EQ(channel->try_enqueue(records.back()), fa::enqueue_result::closed);
  }
}

TEST_F(FileAudioHttps, CanonicalScopeAndEncodedRecordValidationRejectInvalidValues) {
  publish();
  for (const auto &invalid : std::vector<std::string> {
         "{}",
         body().substr(0, body().size() - 1) + R"(,"epoch":"7"})",
         body().substr(0, body().size() - 1) + R"(,"extra":"x"})"
       }) {
    EXPECT_EQ(request(invalid).status, 400);
  }
  auto wrong = body();
  wrong.replace(wrong.find("\"7\""), 3, "\"07\"");
  EXPECT_EQ(request(wrong).status, 400);
  wrong = body();
  wrong.replace(wrong.find("\"7\""), 3, "\"8\"");
  EXPECT_EQ(request(wrong).status, 403);
  for (const auto &value : {"7", "\"-1\"", "\"18446744073709551616\"", "true"}) {
    wrong = body();
    wrong.replace(wrong.find("\"7\""), 3, value);
    EXPECT_EQ(request(wrong).status, 400);
  }
  wrong = body();
  wrong.replace(wrong.find("\"7\""), 3, "\"18446744073709551615\"");
  EXPECT_EQ(request(wrong).status, 403);
  EXPECT_EQ(request(std::string(1025, 'x')).status, 413);
  EXPECT_FALSE(channel->observe().attached);
  for (std::size_t offset : {std::size_t(0), std::size_t(16), std::size_t(64), std::size_t(104), std::size_t(114), std::size_t(121)}) {
    auto bytes = records.front();
    bytes[offset] ^= 1;
    EXPECT_EQ(channel->try_enqueue(bytes), fa::enqueue_result::invalid);
  }
  EXPECT_EQ(channel->try_enqueue(records[1]), fa::enqueue_result::invalid);
  EXPECT_EQ(channel->observe().accepted_records, 0);
}

TEST_F(FileAudioHttps, DisableOwnerBetweenChunksClosesWithoutAFalseEof) {
  publish();
  auto receiving = std::async(std::launch::async, [this] {
    return request(body());
  });
  ASSERT_TRUE(await([this] {
    return channel->observe().attached && !channel->observe().write_pending;
  }));
  ASSERT_EQ(channel->try_enqueue(records.front()), fa::enqueue_result::accepted);
  ASSERT_TRUE(await([this] {
    return channel->observe().sent_records == 1;
  }));
  ASSERT_TRUE(nvhttp::set_client_enabled(uuid, false));
  channel->try_enqueue(records[1]);
  const auto received = receiving.get();
  EXPECT_EQ(received.bytes, std::string(records.front().begin(), records.front().end()));
  EXPECT_FALSE(channel->observe().eof_sent);
  EXPECT_TRUE(channel->observe().closed && channel->observe().drained);
  EXPECT_EQ(channel->observe().reason, "owner_revoked");
}

TEST_F(FileAudioHttps, RevokedOldResponseCannotAcceptOrAdoptNewScope) {
  publish();
  auto old = channel;
  const auto old_request = body();
  auto receiving = std::async(std::launch::async, [this] {
    return request(body());
  });
  ASSERT_TRUE(await([this] {
    return channel->observe().attached && !channel->observe().write_pending;
  }));
  old->revoke("fixture_source_transition");
  EXPECT_EQ(receiving.get().bytes.size(), 0);
  scope.epoch = 8;
  scope.generation = 10;
  publish();
  ASSERT_NE(channel, old);
  EXPECT_EQ(old->try_enqueue(records.front()), fa::enqueue_result::closed);
  EXPECT_EQ(channel->try_enqueue(records.front()), fa::enqueue_result::invalid);
  EXPECT_EQ(request(old_request).status, 403);
  EXPECT_FALSE(channel->observe().attached);
}

TEST_F(FileAudioHttps, ActualClientCancellationDoesNotBecomeFileEof) {
  const auto started = std::chrono::steady_clock::now();
  auto stamp = [&](const char *stage) {
    std::cout << "cancel " << stage << " ms=" << std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - started).count() << '\n';
  };
  publish();
  auto client = make_client(1);
  auto receiving = std::async(std::launch::async, [this, &client] {
    return request(body(), 1, client.get());
  });
  ASSERT_TRUE(await([this] {
    return channel->observe().attached && !channel->observe().write_pending;
  }));
  stamp("attached");
  client->stop();
  stamp("stop_returned");
  EXPECT_NE(receiving.get().code, 0);
  stamp("request_returned");
  channel->revoke("fixture_cancelled");
  stamp("revoked");
  EXPECT_TRUE(channel->observe().drained);
  EXPECT_FALSE(channel->observe().eof_sent);
  client.reset();
  stamp("client_destroyed");
}

TEST_F(FileAudioHttps, LaunchLeaseRevocationRejectsTheNextChunk) {
  publish();
  auto receiving = std::async(std::launch::async, [this] {
    return request(body());
  });
  ASSERT_TRUE(await([this] {
    return channel->observe().attached && !channel->observe().write_pending;
  }));
  current_owner = false;
  channel->try_enqueue(records.front());
  EXPECT_TRUE(receiving.get().bytes.empty());
  EXPECT_EQ(channel->observe().reason, "owner_revoked");
  EXPECT_TRUE(channel->observe().drained);
  EXPECT_FALSE(channel->observe().eof_sent);
}

TEST_F(FileAudioHttps, ActiveProgressOutlivesInitialContentTimeout) {
  publish();
  const auto started = std::chrono::steady_clock::now();
  auto receiving = std::async(std::launch::async, [this] {
    return request(body());
  });
  ASSERT_TRUE(await([this] {
    return channel->observe().attached && !channel->observe().write_pending;
  }));
  for (const auto &record : records) {
    std::this_thread::sleep_for(220ms);
    ASSERT_EQ(channel->try_enqueue(record), fa::enqueue_result::accepted);
  }
  const auto received = receiving.get();
  EXPECT_GT(std::chrono::steady_clock::now() - started, 2s);
  EXPECT_EQ(received.code, 0);
  EXPECT_EQ(received.status, 200);
  EXPECT_EQ(received.bytes, wire);
  EXPECT_TRUE(channel->observe().drained && channel->observe().eof_sent);
}

TEST_F(FileAudioHttps, IdleResponseDeadlineAbortsWithoutEof) {
  publish();
  auto receiving = std::async(std::launch::async, [this] {
    return request(body());
  });
  const auto received = receiving.get();
  EXPECT_NE(received.code, 0);
  EXPECT_TRUE(received.bytes.empty());
  EXPECT_EQ(channel->observe().reason, "response_progress_timeout");
  EXPECT_TRUE(channel->observe().drained);
  EXPECT_FALSE(channel->observe().eof_sent);
}

TEST_F(FileAudioHttps, StalledActualWriteTimesOutAndDrainsItsRealCallback) {
  publish();
  stalled_peer peer(*this);
  ASSERT_TRUE(reach_stalled_write());
  const auto before = channel->observe();
  EXPECT_TRUE(before.write_pending);
  EXPECT_FALSE(before.drained);
  ASSERT_TRUE(await([this] {
    return channel->observe().drained;
  }));
  const auto after = channel->observe();
  EXPECT_EQ(after.reason, "response_progress_timeout");
  EXPECT_FALSE(after.write_pending || after.eof_sent);
  EXPECT_EQ(after.sent_records, before.sent_records);
}

TEST_F(FileAudioHttps, RevokeCancelsStalledActualWriteAndDrainsItsRealCallback) {
  publish();
  stalled_peer peer(*this);
  ASSERT_TRUE(reach_stalled_write());
  EXPECT_TRUE(channel->observe().write_pending);
  channel->revoke("fixture_actual_pending_revoke");
  ASSERT_TRUE(await([this] {
    return channel->observe().drained;
  }));
  EXPECT_EQ(channel->observe().reason, "fixture_actual_pending_revoke");
  EXPECT_FALSE(channel->observe().write_pending || channel->observe().eof_sent);
}

TEST(FileAudioBroker, PendingSendIsBoundedAndRevokeWaitsForActualCallback) {
  fa::broker broker;
  fa::scope scope;
  scope.file_session.fill(1);
  scope.transport.fill(2);
  scope.channel.fill(3);
  auto producer = broker.publish(scope, "explicit-component-fixture", [](const auto &dispatch) {
    dispatch();
    return true;
  });
  ASSERT_TRUE(producer);
  fa::channel::completion pending;
  std::size_t submissions = 0;
  EXPECT_EQ(broker.attach(scope, "explicit-component-fixture", [&](auto, bool headers, auto completion) {
    EXPECT_TRUE(headers);
    ++submissions;
    pending = std::move(completion);
  }),
            fa::attach_result::attached);
  EXPECT_EQ(submissions, 1);
  EXPECT_EQ(broker.attach(scope, "explicit-component-fixture", {}), fa::attach_result::forbidden);
  producer->revoke();
  EXPECT_TRUE(producer->observe().write_pending);
  EXPECT_FALSE(producer->observe().drained);
  EXPECT_FALSE(broker.publish(scope, "explicit-component-fixture", [](const auto &dispatch) {
    dispatch();
    return true;
  }));
  pending(true);
  EXPECT_TRUE(producer->observe().drained);
  EXPECT_FALSE(producer->observe().eof_sent);
  EXPECT_EQ(submissions, 1);
}

/** @brief Run only this private fixture; no host startup, input or audio device is used. */
int main(int argc, char **argv) {
  testing::InitGoogleTest(&argc, argv);
  const auto result = RUN_ALL_TESTS();
  return result;
}
