/**
 * @file native/host/file_session_owner_test.cpp
 * @brief Actual allocation/stop/join gate tests with test-owned workers, no input/network/device startup.
 */
#include <atomic>
#include <chrono>
#include <future>
#include <gtest/gtest.h>
#include <src/config.h>
#include <src/nvhttp.h>
#include <src/rtsp.h>
#include <src/stream.h>
#include <src/utility.h>
#include <stdexcept>

using namespace std::chrono_literals;
namespace fa = quest3d::file_audio;

/** @brief Allocate actual production sessions while replacing only worker bodies with owned waits. */
class FileSessionOwner: public testing::Test {
protected:
  struct owned_session {
    std::shared_ptr<stream::session_t> value;  ///< Exact production allocation.
    bool started = false, joined = false;  ///< Test-owned worker lifecycle.
  };

  std::vector<owned_session> sessions;  ///< All private allocations cleaned up before process exit.
  std::string certificate;  ///< Generated real X509 PEM, never a live paired credential.
  std::string previous_capture;  ///< Restore private process configuration after each case.
  bool previous_audio = false, previous_input = false;  ///< Preserve unrelated test configuration.

  void SetUp() override {
    previous_capture = config::video.capture;
    previous_audio = config::audio.stream;
    previous_input = config::sunshine.quest3d_input;
    config::video.capture = "quest3d";
    config::audio.stream = false;
    config::sunshine.quest3d_input = false;
    config::sunshine.flags[config::flag::FRESH_STATE] = true;
    nvhttp::test_support::reset_client_state();
    certificate = crypto::gen_creds("file-owner-private", 2048).x509;
  }

  void TearDown() override {
    for (auto &item : sessions) {
      if (item.value && item.started && !item.joined) {
        stream::session::stop(*item.value);
        stream::session::test_join_file_owner_workers(*item.value);
      }
    }
    sessions.clear();
    nvhttp::test_support::reset_client_state();
    config::video.capture = previous_capture;
    config::audio.stream = previous_audio;
    config::sunshine.quest3d_input = previous_input;
  }

  /** @brief Execute production alloc with complete copied launch data and valid cipher storage. */
  std::shared_ptr<stream::session_t> allocate(std::string transport = std::string(32, '2'), std::string cert = {}, std::uint32_t launch_id = 101) {
    stream::config_t config {};
    rtsp_stream::launch_session_t launch {};
    launch.client_cert = cert.empty() ? certificate : std::move(cert);
    launch.quest3d_transport_epoch = std::move(transport);
    launch.id = launch_id;
    launch.iv.resize(16);
    launch.gcm_key.resize(16);
    launch.unique_id = "private-file-owner";
    auto session = stream::session::alloc(config, launch);
    sessions.push_back({session});
    return session;
  }

  /** @brief Use the same final RUNNING transition as actual start after constructing owned wait workers. */
  void start(const std::shared_ptr<stream::session_t> &session, std::function<void()> video = {}) {
    stream::session::test_start_file_owner_workers(*session, std::move(video));
    for (auto &item : sessions) {
      if (item.value == session) {
        item.started = true;
      }
    }
  }

  /** @brief Invoke actual production joins without the unrelated input/display side effects of join(). */
  void join(const std::shared_ptr<stream::session_t> &session) {
    stream::session::test_join_file_owner_workers(*session);
    for (auto &item : sessions) {
      if (item.value == session) {
        item.joined = true;
      }
    }
  }

  /** @brief Full independent producer scope with the actual allocated transport. */
  fa::scope scope() const {
    fa::scope value;
    value.file_session.fill(0x11);
    value.transport.fill(0x22);
    value.channel.fill(0x33);
    value.epoch = 7;
    value.generation = 9;
    return value;
  }
};

TEST_F(FileSessionOwner, ActualAllocationIdentityAndNotRunningRefusal) {
  auto session = allocate();
  auto owner = stream::session::file_owner(*session);
  ASSERT_TRUE(owner);
  EXPECT_EQ(owner->identity().certificate, certificate);
  EXPECT_EQ(owner->identity().transport, std::string(32, '2'));
  EXPECT_EQ(owner->identity().launch_id, 101);
  EXPECT_EQ(owner->identity().host_pid, GetCurrentProcessId());
  EXPECT_GT(owner->identity().host_birth, 0);
  EXPECT_EQ(stream::session::state(*session), stream::session::state_e::STOPPED);
  EXPECT_FALSE(owner->begin(scope()));
  start(session);
  auto token = owner->begin(scope());
  ASSERT_TRUE(token);
  EXPECT_EQ(token->identity(), scope());
  EXPECT_EQ(token->serial(), 1);
  unsigned dispatches = 0;
  EXPECT_TRUE(owner->with_current(token, [&] {
    ++dispatches;
  }));
  EXPECT_EQ(dispatches, 1);
  EXPECT_FALSE(owner->release_closed_run(token));
  EXPECT_FALSE(owner->begin(scope()));
}

TEST_F(FileSessionOwner, MissingOrMalformedActualLaunchCannotCreateOwner) {
  EXPECT_FALSE(stream::session::file_owner(*allocate("", certificate)));
  EXPECT_FALSE(stream::session::file_owner(*allocate(std::string(32, '2'), "not a certificate")));
  EXPECT_FALSE(stream::session::file_owner(*allocate(std::string(32, '0'))));
  EXPECT_FALSE(stream::session::file_owner(*allocate(std::string(32, 'Z'))));
  config::video.capture = "unrelated-capture";
  EXPECT_FALSE(stream::session::file_owner(*allocate()));
}

TEST_F(FileSessionOwner, ClosedRunOccupiesSlotUntilExplicitReleaseAndOldTokensRemainDenied) {
  auto session = allocate();
  start(session);
  auto owner = stream::session::file_owner(*session);
  unsigned cancel_requests = 0;
  auto old = owner->begin(scope(), [&] {
    ++cancel_requests;
  });
  ASSERT_TRUE(old);
  EXPECT_TRUE(owner->close(old));
  EXPECT_TRUE(owner->close(old));
  EXPECT_EQ(cancel_requests, 1);
  EXPECT_FALSE(owner->with_current(old, [] {
  }));
  auto next_scope = scope();
  ++next_scope.epoch;
  ++next_scope.generation;
  next_scope.channel.fill(0x44);
  EXPECT_FALSE(owner->begin(next_scope));
  EXPECT_TRUE(owner->release_closed_run(old));
  auto next = owner->begin(next_scope);
  ASSERT_TRUE(next);
  EXPECT_EQ(next->serial(), old->serial() + 1);
  EXPECT_FALSE(owner->close(old));
  EXPECT_FALSE(owner->release_closed_run(old));
  EXPECT_FALSE(owner->with_current(old, [] {
  }));
  EXPECT_TRUE(owner->with_current(next, [] {
  }));
  EXPECT_EQ(old->identity(), scope());
}

TEST_F(FileSessionOwner, OtherTransportAndEvenEqualMetadataAllocationCannotAdoptToken) {
  auto first = allocate();
  auto equal = allocate();
  auto other = allocate(std::string(32, '5'), certificate, 102);
  start(first);
  start(equal);
  start(other);
  auto owner = stream::session::file_owner(*first);
  auto same_identity = stream::session::file_owner(*equal);
  auto changed = stream::session::file_owner(*other);
  auto token = owner->begin(scope());
  ASSERT_TRUE(token);
  EXPECT_FALSE(changed->begin(scope()));
  auto other_scope = scope();
  other_scope.transport.fill(0x55);
  auto other_token = changed->begin(other_scope);
  auto equal_token = same_identity->begin(scope());
  ASSERT_TRUE(other_token && equal_token);
  EXPECT_FALSE(changed->with_current(token, [] {
  }));
  EXPECT_FALSE(same_identity->with_current(token, [] {
  }));
  EXPECT_FALSE(owner->with_current(equal_token, [] {
  }));
  EXPECT_FALSE(owner->close(other_token));
  EXPECT_TRUE(changed->with_current(other_token, [] {
  }));
}

TEST_F(FileSessionOwner, HeldDispatchSerializesStopAndCancellationHookRunsAfterUnlock) {
  auto session = allocate();
  start(session);
  auto owner = stream::session::file_owner(*session);
  fa::session_owner::run token;
  bool hook_observed_closed = false;
  token = owner->begin(scope(), [&] {
    hook_observed_closed = !owner->with_current(token, [] {
    });
  });
  ASSERT_TRUE(token);
  std::promise<void> entered, release;
  auto gate = release.get_future().share();
  auto dispatch = std::async(std::launch::async, [&] {
    return owner->with_current(token, [&] {
      entered.set_value();
      gate.wait();
    });
  });
  auto cleanup = util::fail_guard([&] {
    if (gate.wait_for(0ms) != std::future_status::ready) {
      release.set_value();
    }
  });
  ASSERT_EQ(entered.get_future().wait_for(2s), std::future_status::ready);
  auto stop = std::async(std::launch::async, [&] {
    stream::session::stop(*session);
  });
  EXPECT_EQ(stop.wait_for(20ms), std::future_status::timeout);
  EXPECT_EQ(stream::session::state(*session), stream::session::state_e::RUNNING);
  release.set_value();
  EXPECT_TRUE(dispatch.get());
  ASSERT_EQ(stop.wait_for(2s), std::future_status::ready);
  stop.get();
  EXPECT_TRUE(hook_observed_closed);
  EXPECT_EQ(stream::session::state(*session), stream::session::state_e::STOPPING);
  EXPECT_FALSE(owner->with_current(token, [] {
  }));
  EXPECT_FALSE(owner->begin(scope()));
}

TEST_F(FileSessionOwner, SlotReleaseIsNotWorkerRetirementAndJoinHoldsNoLifetimeGate) {
  auto session = allocate();
  std::promise<void> video_entered, release;
  auto gate = release.get_future().share();
  auto cleanup = util::fail_guard([&] {
    if (gate.wait_for(0ms) != std::future_status::ready) {
      release.set_value();
    }
  });
  start(session, [&] {
    video_entered.set_value();
    gate.wait();
  });
  auto owner = stream::session::file_owner(*session);
  auto token = owner->begin(scope());
  ASSERT_TRUE(token);
  stream::session::stop(*session);
  ASSERT_EQ(video_entered.get_future().wait_for(2s), std::future_status::ready);
  auto joined = std::async(std::launch::async, [&] {
    join(session);
  });
  EXPECT_EQ(joined.wait_for(20ms), std::future_status::timeout);
  EXPECT_FALSE(owner->with_current(token, [] {
  }));
  // Deliberately release before the held join only to prove this API is not a cleanup receipt.
  EXPECT_TRUE(owner->release_closed_run(token));
  EXPECT_EQ(joined.wait_for(0ms), std::future_status::timeout);
  EXPECT_FALSE(owner->begin(scope()));
  release.set_value();
  ASSERT_EQ(joined.wait_for(2s), std::future_status::ready);
  joined.get();
}

TEST_F(FileSessionOwner, WeakOwnerCannotKeepReleasedActualSessionAlive) {
  auto session = allocate();
  start(session);
  std::weak_ptr<stream::session_t> weak = session;
  auto owner = stream::session::file_owner(*session);
  auto token = owner->begin(scope());
  stream::session::stop(*session);
  join(session);
  sessions.clear();
  session.reset();
  EXPECT_TRUE(weak.expired());
  EXPECT_FALSE(owner->with_current(token, [] {
  }));
  EXPECT_FALSE(owner->begin(scope()));
  EXPECT_FALSE(owner->close(token));
  EXPECT_FALSE(owner->release_closed_run(token));
  EXPECT_EQ(owner->identity().launch_id, 101);
}

TEST_F(FileSessionOwner, ThrowingCancelHookLeavesAcceptanceClosedAndStopStillEndsWorkers) {
  auto session = allocate();
  start(session);
  auto owner = stream::session::file_owner(*session);
  unsigned requested = 0;
  auto token = owner->begin(scope(), [&] {
    ++requested;
    throw std::runtime_error("private cancellation failure");
  });
  EXPECT_FALSE(owner->close(token));
  EXPECT_FALSE(owner->with_current(token, [] {
  }));
  EXPECT_FALSE(owner->begin(scope()));
  stream::session::stop(*session);
  join(session);
  EXPECT_EQ(requested, 1);
}

TEST_F(FileSessionOwner, ActualPairedAuthWrapperIsRequiredBeyondRunningSessionLease) {
  auto session = allocate();
  start(session);
  auto owner = stream::session::file_owner(*session);
  auto token = owner->begin(scope());
  ASSERT_TRUE(token);
  const auto uuid = nvhttp::test_support::add_client("private-owner", certificate, true);
  fa::broker broker;
  auto lease = [owner, token](const auto &dispatch) {
    return owner->with_current(token, dispatch);
  };
  auto channel = nvhttp::test_support::publish_test_file_audio_binding(broker, scope(), certificate, lease);
  ASSERT_TRUE(channel);
  EXPECT_TRUE(nvhttp::set_client_enabled(uuid, false));
  EXPECT_TRUE(owner->with_current(token, [] {
  }));  // Only the session half remains live.
  EXPECT_EQ(broker.attach(scope(), certificate, [](auto, bool, auto) {
  }),
            fa::attach_result::forbidden);
  EXPECT_EQ(channel->observe().reason, "owner_revoked");
  EXPECT_TRUE(channel->observe().drained);
  EXPECT_FALSE(nvhttp::test_support::publish_test_file_audio_binding(broker, scope(), certificate, lease));
}

TEST(FileSessionOwnerRegression, ExistingPointerStopBarrierStillUsesActualSessionGate) {
  EXPECT_TRUE(stream::session::test_pointer_stop_barrier());
}

/** @brief Run only private owner tests; no live services, capture, input or display are started. */
int main(int argc, char **argv) {
  testing::InitGoogleTest(&argc, argv);
  return RUN_ALL_TESTS();
}
