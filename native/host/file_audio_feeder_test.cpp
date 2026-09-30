/** @file native/host/file_audio_feeder_test.cpp
 * @brief Actual Windows mapping and production broker transfer tests; fixture owner grants are not product authority.
 */
#include "src/platform/windows/quest3d_file_audio_feeder.h"

#include <atomic>
#include <fstream>
#include <future>
#include <gtest/gtest.h>
#include <thread>

namespace ipc = quest3d::file_audio::ipc;
namespace fa = quest3d::file_audio;

/** @brief Write one fixture uint field using the actual wire convention. */
void put(std::uint8_t *bytes, std::uint64_t value, std::size_t count) {
  for (std::size_t i = 0; i < count; ++i) {
    bytes[i] = static_cast<std::uint8_t>(value >> (i * 8));
  }
}

/** @brief Real Windows producer plus actual bounded channel with held asynchronous completion. */
class FileAudioFeeder: public testing::Test {
protected:
  /** @brief Create unique test-only source objects and publish a fixture-authorized real broker channel. */
  void SetUp() override {
    static std::atomic<std::uint64_t> serial = 1;
    identity.file_session.fill(0x11);
    identity.transport.fill(0x22);
    identity.channel.fill(0x33);
    put(identity.channel.data(), (std::uint64_t(GetCurrentProcessId()) << 32) | serial++, 8);
    identity.epoch = 7;
    identity.generation = 9;
    constexpr wchar_t hex[] = L"0123456789abcdef";
    name = L"Local\\Quest3D.FileAudio.v1.";
    for (auto byte : identity.channel) {
      name += hex[byte >> 4];
      name += hex[byte & 15];
    }
    mutex = CreateMutexW(nullptr, FALSE, (name + L".Mutex").c_str());
    ASSERT_NE(mutex, nullptr);
    ASSERT_NE(GetLastError(), DWORD(ERROR_ALREADY_EXISTS));
    mapping = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0, ipc::mapping_bytes, name.c_str());
    ASSERT_NE(mapping, nullptr);
    ASSERT_NE(GetLastError(), DWORD(ERROR_ALREADY_EXISTS));
    memory = static_cast<ipc::memory *>(MapViewOfFile(mapping, FILE_MAP_ALL_ACCESS, 0, 0, ipc::mapping_bytes));
    ASSERT_NE(memory, nullptr);
    *memory = {};
    auto &h = memory->state;
    h.magic = {'Q', '3', 'D', 'F', 'A', 'I', '1', 0};
    h.version = 1;
    h.header_bytes = 256;
    h.slots = 8;
    h.slot_size = 4096;
    h.producer_pid = GetCurrentProcessId();
    FILETIME birth {}, exit {}, kernel {}, user {};
    ASSERT_TRUE(GetProcessTimes(GetCurrentProcess(), &birth, &exit, &kernel, &user));
    h.producer_creation = (std::uint64_t(birth.dwHighDateTime) << 32) | birth.dwLowDateTime;
    h.file_session = identity.file_session;
    h.transport = identity.transport;
    h.channel = identity.channel;
    h.epoch = identity.epoch;
    h.generation = identity.generation;
    destination = broker.publish(identity, "fixture-owner", [this](const auto &dispatch) {
      if (!owner_live) {
        return false;
      }
      if (owner_hook) {
        owner_hook();
      }
      dispatch();
      return true;
    });
    ASSERT_TRUE(destination);
  }

  /** @brief Release any held actual channel callback before destroying its owning fixture. */
  void TearDown() override {
    destination->revoke("fixture_end");
    if (pending) {
      complete(false);
    }
    if (memory) {
      EXPECT_TRUE(UnmapViewOfFile(memory));
    }
    if (mapping) {
      EXPECT_TRUE(CloseHandle(mapping));
    }
    if (mutex) {
      EXPECT_TRUE(CloseHandle(mutex));
    }
  }

  /** @brief One finite PCM fixture record of a chosen original frame count or explicit EOF. */
  std::vector<std::uint8_t> wire(std::uint64_t sequence, std::uint16_t frames = 480) const {
    std::vector<std::uint8_t> bytes(128 + frames * 8);
    auto *p = bytes.data();
    std::memcpy(p, "Q3DPCM1\0", 8);
    put(p + 8, 1, 2);
    put(p + 10, 128, 2);
    put(p + 12, frames * 8, 4);
    std::memcpy(p + 16, identity.file_session.data(), 16);
    std::memcpy(p + 32, identity.transport.data(), 16);
    std::memcpy(p + 48, identity.channel.data(), 16);
    put(p + 64, identity.epoch, 8);
    put(p + 72, identity.generation, 8);
    put(p + 80, sequence, 8);
    put(p + 88, sequence, 8);
    put(p + 96, sequence * 10000000, 8);
    put(p + 104, 48000, 4);
    put(p + 108, frames, 2);
    put(p + 112, frames, 2);
    p[114] = 2;
    p[115] = 1;
    put(p + 116, frames ? 0 : 1, 4);
    return bytes;
  }

  /** @brief Publish exactly one original wire record under the real named transaction mutex. */
  void append(std::span<const std::uint8_t> bytes) {
    ASSERT_EQ(WaitForSingleObject(mutex, 50), DWORD(WAIT_OBJECT_0));
    auto &h = memory->state;
    ASSERT_LT(h.tail - h.head, 8u);
    auto &record = memory->records[h.tail % 8];
    record = {};
    record.sequence = h.tail;
    record.length = static_cast<std::uint32_t>(bytes.size());
    std::copy(bytes.begin(), bytes.end(), record.payload.begin());
    record.digest = ipc::digest(bytes);
    ++h.tail;
    h.produced_records = h.tail;
    h.produced_frames += fa::read_le(bytes.data() + 112, 2);
    h.eof_produced = fa::read_le(bytes.data() + 116, 4) == 1;
    ASSERT_TRUE(ReleaseMutex(mutex));
  }

  /** @brief Attach a real broker sender whose completion is intentionally controlled by this fixture. */
  void attach() {
    ASSERT_EQ(broker.attach(identity, "fixture-owner", [this](auto bytes, bool headers, auto done) {
      EXPECT_FALSE(pending);
      pending = std::move(done);
      pending_bytes = std::move(bytes);
      pending_headers = headers;
    },
                            {{}, [this] {
                               ++abort_requests;
                             }}),
              fa::attach_result::attached);
    ASSERT_TRUE(pending);
    ASSERT_TRUE(pending_headers);
  }

  /** @brief Invoke only the actual saved asynchronous completion, without fabricating a broker ACK. */
  void complete(bool success = true) {
    ASSERT_TRUE(pending);
    auto done = std::move(pending);
    pending = {};
    if (success && pending_bytes) {
      delivered.emplace_back(pending_bytes->begin(), pending_bytes->end());
    }
    pending_bytes.reset();
    done(success);
  }

  /** @brief Mutate a producer field under its real transaction mutex. */
  template<class F>
  void locked(F change) {
    ASSERT_EQ(WaitForSingleObject(mutex, 50), DWORD(WAIT_OBJECT_0));
    change();
    EXPECT_TRUE(ReleaseMutex(mutex));
  }

  fa::scope identity;  ///< Unique immutable fixture scope.
  std::wstring name;  ///< Owned native names.
  HANDLE mutex = nullptr, mapping = nullptr;  ///< Actual Windows producer handles.
  ipc::memory *memory = nullptr;  ///< Actual mapped header/slots.
  fa::broker broker;  ///< Production broker with one fixture-authorized binding.
  std::shared_ptr<fa::channel> destination;  ///< Exact channel bound to the feeder.
  bool owner_live = true;  ///< Explicit fixture permission, never wired into production.
  std::function<void()> owner_hook;  ///< Controlled race at the actual channel owner dispatch boundary.
  fa::channel::completion pending;  ///< Real callback currently owned by the fake network sender.
  fa::channel::record pending_bytes;  ///< Exactly one actual pending write record.
  bool pending_headers = false;  ///< Whether the held callback is for HTTP headers.
  int abort_requests = 0;  ///< Actual abort-hook calls, not remote/TLS drain.
  std::vector<std::vector<std::uint8_t>> delivered;  ///< Fixture-only collection of successful bounded sends.
};

TEST_F(FileAudioFeeder, EightIPCRowsFourBrokerRecordsAndHeldWrite) {
  for (int i = 0; i < 8; ++i) {
    append(wire(i));
  }
  ipc::feeder feeder;
  ASSERT_TRUE(feeder.open(identity, destination));
  attach();  // Hold the actual headers callback while filling four broker entries.
  for (int i = 0; i < 4; ++i) {
    EXPECT_EQ(feeder.step(), ipc::step_result::handed_off);
  }
  EXPECT_EQ(memory->state.head, 4u);
  EXPECT_EQ(memory->state.tail, 8u);
  EXPECT_EQ(destination->observe().queued_records, 4u);
  for (int i = 0; i < 10; ++i) {
    EXPECT_EQ(feeder.step(), ipc::step_result::backpressure);
  }
  EXPECT_EQ(memory->state.head, 4u);
  EXPECT_EQ(destination->observe().accepted_records, 4u);
  complete();  // Header completion submits the real first data write; total remains four.
  EXPECT_EQ(feeder.step(), ipc::step_result::backpressure);
  EXPECT_TRUE(destination->observe().write_pending);
  complete();
  EXPECT_EQ(feeder.step(), ipc::step_result::handed_off);
  EXPECT_EQ(memory->state.head, 5u);
  EXPECT_EQ(feeder.snapshot().accepted_records, 5u);
  EXPECT_EQ(feeder.snapshot().committed_records, 5u);
  EXPECT_TRUE(feeder.close());
  EXPECT_EQ(feeder.step(), ipc::step_result::closed);
  EXPECT_TRUE(destination->observe().write_pending);
  EXPECT_FALSE(destination->observe().drained);
  auto receipt = feeder.retire();
  ASSERT_TRUE(receipt);
  EXPECT_TRUE(receipt->facts().resources_released);
  EXPECT_FALSE(destination->observe().drained);
  complete(false);
  EXPECT_TRUE(destination->observe().drained);
}

TEST_F(FileAudioFeeder, PartialRecordAndEOFRemainExactUntilActualCompletion) {
  const auto first = wire(0, 203), eof = wire(1, 0);
  append(first);
  append(eof);
  ipc::feeder feeder;
  ASSERT_TRUE(feeder.open(identity, destination));
  attach();
  complete();
  EXPECT_EQ(feeder.step(), ipc::step_result::handed_off);
  EXPECT_EQ(feeder.step(), ipc::step_result::eof_handed);
  EXPECT_EQ(memory->state.handed_frames, 203u);
  EXPECT_TRUE(feeder.snapshot().eof_handed);
  EXPECT_FALSE(destination->observe().eof_sent);
  EXPECT_EQ(feeder.step(), ipc::step_result::closed);
  complete();
  ASSERT_TRUE(pending);
  EXPECT_EQ(pending_bytes->size(), 128u);
  EXPECT_FALSE(destination->observe().eof_sent);
  complete();
  EXPECT_TRUE(destination->observe().eof_sent);
  EXPECT_TRUE(destination->observe().drained);
  ASSERT_EQ(delivered.size(), 2u);
  EXPECT_EQ(delivered[0], first);
  EXPECT_EQ(delivered[1], eof);
  EXPECT_TRUE(feeder.retire());
}

TEST_F(FileAudioFeeder, OwnerDispatchRunsOutsideActualIPCMutex) {
  append(wire(0));
  ipc::feeder feeder;
  ASSERT_TRUE(feeder.open(identity, destination));
  bool checked = false;
  owner_hook = [&] {
    std::thread probe([&] {
      const auto result = WaitForSingleObject(mutex, 100);
      EXPECT_EQ(result, DWORD(WAIT_OBJECT_0));
      if (result == WAIT_OBJECT_0) {
        checked = true;
        EXPECT_TRUE(ReleaseMutex(mutex));
      }
    });
    probe.join();
  };
  EXPECT_EQ(feeder.step(), ipc::step_result::handed_off);
  EXPECT_TRUE(checked);
  owner_hook = {};
}

TEST_F(FileAudioFeeder, AcceptedThenChangedBytesCannotCommitOrReplay) {
  append(wire(0));
  ipc::feeder feeder;
  ASSERT_TRUE(feeder.open(identity, destination));
  owner_hook = [&] {
    locked([&] {
      auto &s = memory->records[0];
      s.payload[128] = 1;
      s.digest = ipc::digest(std::span(s.payload).first(s.length));
    });
  };
  EXPECT_EQ(feeder.step(), ipc::step_result::failed);
  EXPECT_EQ(memory->state.head, 0u);
  EXPECT_EQ(destination->observe().accepted_records, 1u);
  EXPECT_TRUE(destination->observe().closed);
  EXPECT_TRUE(feeder.snapshot().accepted_not_committed);
  EXPECT_EQ(feeder.snapshot().error, ipc::feeder_error::commit_failed);
  owner_hook = {};
  EXPECT_EQ(feeder.step(), ipc::step_result::failed);
  EXPECT_EQ(destination->observe().accepted_records, 1u);
}

TEST_F(FileAudioFeeder, OwnerRevocationAfterQueueAcceptDoesNotCommit) {
  append(wire(0));
  ipc::feeder feeder;
  ASSERT_TRUE(feeder.open(identity, destination));
  owner_live = false;
  EXPECT_EQ(feeder.step(), ipc::step_result::failed);
  EXPECT_EQ(memory->state.head, 0u);
  EXPECT_TRUE(feeder.snapshot().accepted_not_committed);
  EXPECT_EQ(feeder.snapshot().error, ipc::feeder_error::channel_closed);
  EXPECT_EQ(destination->observe().reason, "owner_revoked");
}

TEST_F(FileAudioFeeder, DispatchExceptionCannotReplayAlreadyOwnedBytes) {
  append(wire(0));
  ipc::feeder feeder;
  ASSERT_TRUE(feeder.open(identity, destination));
  owner_hook = [] {
    throw std::runtime_error("fixture owner dispatch failure");
  };
  EXPECT_EQ(feeder.step(), ipc::step_result::failed);
  EXPECT_EQ(memory->state.head, 0u);
  EXPECT_TRUE(feeder.snapshot().accepted_not_committed);
  EXPECT_EQ(feeder.snapshot().error, ipc::feeder_error::dispatch_exception);
  owner_hook = {};
  EXPECT_EQ(feeder.step(), ipc::step_result::failed);
  EXPECT_EQ(destination->observe().accepted_records, 1u);
}

TEST_F(FileAudioFeeder, ReaderCorruptionAndAlreadyClosedChannelAreTerminal) {
  append(wire(0));
  ipc::feeder feeder;
  ASSERT_TRUE(feeder.open(identity, destination));
  locked([&] {
    memory->records[0].payload[128] = 1;
  });
  EXPECT_EQ(feeder.step(), ipc::step_result::failed);
  EXPECT_EQ(feeder.snapshot().error, ipc::feeder_error::reader_failed);
  EXPECT_EQ(destination->observe().accepted_records, 0u);
  EXPECT_TRUE(destination->observe().closed);
}

TEST_F(FileAudioFeeder, ScopeMismatchDoesNotRevokeAnotherBinding) {
  auto other = identity;
  ++other.epoch;
  ipc::feeder feeder;
  EXPECT_FALSE(feeder.open(other, destination));
  EXPECT_EQ(feeder.snapshot().error, ipc::feeder_error::invalid_binding);
  EXPECT_FALSE(destination->observe().closed);
}

TEST_F(FileAudioFeeder, SequenceConflictRevokesExactChannel) {
  append(wire(0));
  ipc::feeder feeder;
  ASSERT_TRUE(feeder.open(identity, destination));
  ASSERT_EQ(destination->try_enqueue(wire(0)), fa::enqueue_result::accepted);  // Deliberate competing-producer violation.
  EXPECT_EQ(feeder.step(), ipc::step_result::failed);
  EXPECT_EQ(feeder.snapshot().error, ipc::feeder_error::channel_invalid);
  EXPECT_EQ(memory->state.head, 0u);
  EXPECT_TRUE(destination->observe().closed);
}

TEST_F(FileAudioFeeder, RetiredOldFeederDoesNotTouchNewBrokerBinding) {
  ipc::feeder feeder;
  ASSERT_TRUE(feeder.open(identity, destination));
  ASSERT_TRUE(feeder.retire());
  auto replacement = identity;
  ++replacement.generation;
  auto current = broker.publish(replacement, "fixture-owner", [](const auto &dispatch) {
    dispatch();
    return true;
  });
  ASSERT_TRUE(current);
  EXPECT_EQ(feeder.step(), ipc::step_result::closed);
  feeder.close();
  feeder.retire();
  EXPECT_FALSE(current->observe().closed);
}

TEST_F(FileAudioFeeder, CloseBusyMutexKeepsSourceForActualRetry) {
  ipc::feeder feeder;
  ASSERT_TRUE(feeder.open(identity, destination));
  std::promise<void> acquired, release;
  auto resume = release.get_future();
  std::thread hold([&] {
    EXPECT_EQ(WaitForSingleObject(mutex, 1000), DWORD(WAIT_OBJECT_0));
    acquired.set_value();
    resume.wait();
    EXPECT_TRUE(ReleaseMutex(mutex));
  });
  acquired.get_future().wait();
  EXPECT_FALSE(feeder.close());
  EXPECT_FALSE(feeder.retire());
  EXPECT_FALSE(feeder.snapshot().source.resources_released);
  EXPECT_FALSE(feeder.snapshot().accepting);
  EXPECT_TRUE(destination->observe().closed);
  release.set_value();
  hold.join();
  EXPECT_TRUE(feeder.retire());
  EXPECT_EQ(memory->state.consumer_closed, 1u);
}

TEST_F(FileAudioFeeder, IdleAndOpenFailureNeverMintAReadyState) {
  ipc::feeder feeder;
  EXPECT_EQ(feeder.step(), ipc::step_result::closed);
  ASSERT_TRUE(feeder.open(identity, destination));
  EXPECT_EQ(feeder.step(), ipc::step_result::idle);
  EXPECT_FALSE(feeder.open(identity, destination));
  EXPECT_EQ(feeder.snapshot().error, ipc::feeder_error::already_attempted);
  EXPECT_TRUE(destination->observe().closed);
}

TEST_F(FileAudioFeeder, ClosedBindingAndMissingSourceRejectWithoutHandoff) {
  ipc::feeder empty;
  EXPECT_FALSE(empty.open(identity, {}));
  EXPECT_EQ(empty.snapshot().accepted_records, 0u);
  ipc::reader occupied;
  ASSERT_TRUE(occupied.open(identity));
  ipc::feeder duplicate;
  EXPECT_FALSE(duplicate.open(identity, destination));
  EXPECT_EQ(duplicate.snapshot().error, ipc::feeder_error::reader_open);
  EXPECT_TRUE(destination->observe().closed);
  ipc::feeder already_closed;
  EXPECT_FALSE(already_closed.open(identity, destination));
  EXPECT_EQ(already_closed.snapshot().error, ipc::feeder_error::channel_closed);
}

TEST_F(FileAudioFeeder, ExternalChannelCloseStopsAnAlreadyOpenedFeeder) {
  ipc::feeder feeder;
  ASSERT_TRUE(feeder.open(identity, destination));
  destination->revoke("external_cancel");
  EXPECT_EQ(feeder.step(), ipc::step_result::failed);
  EXPECT_EQ(feeder.snapshot().error, ipc::feeder_error::channel_closed);
  EXPECT_EQ(destination->observe().reason, "external_cancel");
  EXPECT_EQ(memory->state.head, 0u);
}

TEST_F(FileAudioFeeder, ActualFile5003FramesAndPartialEOFKeepAllPCMBytes) {
  std::ifstream source(std::string(FILE_AUDIO_VECTOR_DIR) + "/first.wire", std::ios::binary);
  std::ifstream pcm_file(std::string(FILE_AUDIO_VECTOR_DIR) + "/first.pcm", std::ios::binary);
  ASSERT_TRUE(source);
  ASSERT_TRUE(pcm_file);
  std::vector<std::uint8_t> original((std::istreambuf_iterator<char>(source)), {});
  std::vector<std::uint8_t> pcm((std::istreambuf_iterator<char>(pcm_file)), {});
  ASSERT_LT(original.size(), 65536u);
  ASSERT_EQ(pcm.size(), 5003u * 8);
  ipc::feeder feeder;
  ASSERT_TRUE(feeder.open(identity, destination));
  attach();
  complete();
  std::size_t at = 0, rows = 0;
  bool eof = false;
  while (at < original.size()) {
    ASSERT_GE(original.size() - at, 128u);
    const auto length = 128 + fa::read_le(original.data() + at + 12, 4);
    ASSERT_LE(length, fa::max_record_bytes);
    ASSERT_LE(length, original.size() - at);
    std::vector<std::uint8_t> bytes(original.begin() + at, original.begin() + at + length);
    // Only the fresh fixture channel changes; original PCM/PTS/sequence/partial count stay exact.
    std::copy(identity.channel.begin(), identity.channel.end(), bytes.begin() + 48);
    eof = fa::read_le(bytes.data() + 116, 4) == 1;
    append(bytes);
    EXPECT_EQ(feeder.step(), eof ? ipc::step_result::eof_handed : ipc::step_result::handed_off);
    ASSERT_TRUE(pending_bytes);
    EXPECT_EQ(*pending_bytes, bytes);
    complete();
    at += length;
    ++rows;
  }
  EXPECT_TRUE(eof);
  EXPECT_EQ(rows, 12u);
  EXPECT_EQ(memory->state.handed_frames, 5003u);
  ASSERT_EQ(delivered.size(), 12u);
  EXPECT_EQ(fa::read_le(delivered[10].data() + 112, 2), 203u);
  EXPECT_EQ(delivered[11].size(), 128u);
  std::vector<std::uint8_t> received;
  for (const auto &bytes : delivered) {
    received.insert(received.end(), bytes.begin() + 128, bytes.end());
  }
  EXPECT_EQ(received, pcm);
  EXPECT_TRUE(destination->observe().eof_sent);
  EXPECT_TRUE(feeder.retire());
}

/** @brief Run this isolated production feeder fixture. */
int main(int argc, char **argv) {
  testing::InitGoogleTest(&argc, argv);
  return RUN_ALL_TESTS();
}
