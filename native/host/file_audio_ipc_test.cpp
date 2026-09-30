/** @file native/host/file_audio_ipc_test.cpp
 * @brief Real Windows mapping/handle tests of the production FileAudio IPC reader.
 */
#include "src/platform/windows/quest3d_file_audio_ipc_reader.h"

#include <atomic>
#include <future>
#include <gtest/gtest.h>
#include <thread>

namespace ipc = quest3d::file_audio::ipc;
namespace fa = quest3d::file_audio;

/** @brief Write a fixture wire integer with the actual protocol endian convention. */
void put(std::uint8_t *bytes, std::uint64_t value, std::size_t count) {
  for (std::size_t i = 0; i < count; ++i) {
    bytes[i] = static_cast<std::uint8_t>(value >> (i * 8));
  }
}

/** @brief Build one finite fixture record without modifying production validation. */
ipc::slot record(const fa::scope &identity, std::uint64_t sequence, bool eof = false) {
  ipc::slot slot {};
  slot.sequence = sequence;
  slot.length = eof ? 128 : 136;
  auto *p = slot.payload.data();
  std::memcpy(p, "Q3DPCM1\0", 8);
  put(p + 8, 1, 2);
  put(p + 10, 128, 2);
  put(p + 12, eof ? 0 : 8, 4);
  std::memcpy(p + 16, identity.file_session.data(), 16);
  std::memcpy(p + 32, identity.transport.data(), 16);
  std::memcpy(p + 48, identity.channel.data(), 16);
  put(p + 64, identity.epoch, 8);
  put(p + 72, identity.generation, 8);
  put(p + 80, sequence, 8);
  put(p + 104, 48000, 4);
  put(p + 108, eof ? 0 : 1, 2);
  put(p + 112, eof ? 0 : 1, 2);
  p[114] = 2;
  p[115] = 1;
  put(p + 116, eof ? 1 : 0, 4);
  slot.digest = ipc::digest(std::span(slot.payload).first(slot.length));
  return slot;
}

/** @brief Real producer-owned Windows fixture objects, separate from any live mapping. */
class FileAudioIPC: public testing::Test {
protected:
  /** @brief Allocate uniquely named mapping and mutex with the real test producer birth. */
  void SetUp() override {
    static std::atomic<std::uint64_t> unique = 1;
    identity.file_session.fill(0x11);
    identity.transport.fill(0x22);
    identity.channel.fill(0x33);
    put(identity.channel.data(), (std::uint64_t(GetCurrentProcessId()) << 32) | unique++, 8);
    identity.epoch = 7;
    identity.generation = 9;
    constexpr wchar_t hex[] = L"0123456789abcdef";
    name = L"Local\\Quest3D.FileAudio.v1.";
    for (auto b : identity.channel) {
      name += hex[b >> 4];
      name += hex[b & 15];
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
  }

  /** @brief Destroy fixture-owned handles only after each local reader has left its scope. */
  void TearDown() override {
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

  /** @brief Publish one fixture record under the same actual named transaction mutex. */
  void append(bool eof = false) {
    ASSERT_EQ(WaitForSingleObject(mutex, 50), DWORD(WAIT_OBJECT_0));
    auto &h = memory->state;
    ASSERT_LT(h.tail - h.head, 8u);
    memory->records[h.tail % 8] = record(identity, h.tail, eof);
    ++h.tail;
    h.produced_records = h.tail;
    h.produced_frames += eof ? 0 : 1;
    h.eof_produced = eof ? 1 : 0;
    ASSERT_TRUE(ReleaseMutex(mutex));
  }

  /** @brief Corrupt or inspect shared state only inside the production transaction lock. */
  template<typename F>
  void locked(F change) {
    ASSERT_EQ(WaitForSingleObject(mutex, 50), DWORD(WAIT_OBJECT_0));
    change();
    ASSERT_TRUE(ReleaseMutex(mutex));
  }

  fa::scope identity;  ///< Unique fixed test scope.
  std::wstring name;  ///< Owned object prefix.
  HANDLE mutex = nullptr, mapping = nullptr;  ///< Actual native owner handles.
  ipc::memory *memory = nullptr;  ///< Fixed-size native view.
};

TEST_F(FileAudioIPC, EmptyAndExactEOFHandoff) {
  ipc::reader reader;
  ASSERT_TRUE(reader.open(identity));
  EXPECT_FALSE(reader.peek_record());
  append();
  append(true);
  auto first = reader.peek_record();
  ASSERT_TRUE(first);
  EXPECT_EQ(first->sequence(), 0u);
  EXPECT_EQ(first->identity(), identity);
  EXPECT_EQ(memory->state.head, 0u);
  EXPECT_EQ(memory->state.handed_frames, 0u);
  auto retry = reader.peek_record();
  ASSERT_TRUE(retry);
  EXPECT_TRUE(std::ranges::equal(first->bytes(), retry->bytes()));
  ASSERT_TRUE(reader.commit_handoff(*retry));
  EXPECT_EQ(memory->state.head, 1u);
  EXPECT_EQ(memory->state.handed_frames, 1u);
  auto eof = reader.peek_record();
  ASSERT_TRUE(eof);
  EXPECT_EQ(eof->bytes().size(), 128u);
  ASSERT_TRUE(reader.commit_handoff(*eof));
  EXPECT_TRUE(reader.snapshot().eof_handed);
  EXPECT_FALSE(reader.peek_record());
  auto retired = reader.retire();
  ASSERT_TRUE(retired);
  EXPECT_TRUE(retired->facts().stopped);
  EXPECT_TRUE(retired->facts().resources_released);
  EXPECT_EQ(memory->state.consumer_closed, 1u);
  EXPECT_EQ(memory->state.consumer_pid, GetCurrentProcessId());
  EXPECT_FALSE(reader.retire());
}

TEST_F(FileAudioIPC, DuplicateConsumerAndDifferentThreadRetirement) {
  ipc::reader first;
  ASSERT_TRUE(first.open(identity));
  ipc::reader duplicate;
  EXPECT_FALSE(duplicate.open(identity));
  EXPECT_EQ(duplicate.snapshot().error, ipc::reader_error::duplicate_consumer);
  EXPECT_FALSE(duplicate.retire());
  EXPECT_TRUE(duplicate.snapshot().resources_released);
  auto result = std::async(std::launch::async, [&] {
    return first.retire().has_value();
  });
  EXPECT_TRUE(result.get());  // .Consumer was never thread-owned.
  EXPECT_EQ(memory->state.consumer_closed, 1u);
}

TEST_F(FileAudioIPC, BackpressureDoesNotConsumeAndReplayCannotAdvance) {
  for (int i = 0; i < 8; ++i) {
    append();
  }
  ipc::reader reader;
  ASSERT_TRUE(reader.open(identity));
  auto offer = reader.peek_record();
  ASSERT_TRUE(offer);
  for (int i = 0; i < 20; ++i) {
    EXPECT_TRUE(reader.peek_record());
    EXPECT_EQ(memory->state.head, 0u);
  }
  ASSERT_TRUE(reader.commit_handoff(*offer));
  EXPECT_FALSE(reader.commit_handoff(*offer));
  EXPECT_EQ(memory->state.head, 1u);
  EXPECT_EQ(reader.snapshot().error, ipc::reader_error::stale_offer);
}

TEST_F(FileAudioIPC, SameScopeOtherReaderTokenCannotCommit) {
  append();
  ipc::reader reader;
  ASSERT_TRUE(reader.open(identity));
  auto old = reader.peek_record();
  ASSERT_TRUE(old);
  ASSERT_TRUE(reader.stop());
  EXPECT_FALSE(reader.commit_handoff(*old));
  EXPECT_EQ(memory->state.head, 0u);
}

TEST_F(FileAudioIPC, RehashedPendingBytesRejected) {
  append();
  ipc::reader reader;
  ASSERT_TRUE(reader.open(identity));
  auto offered = reader.peek_record();
  ASSERT_TRUE(offered);
  locked([&] {
    auto &s = memory->records[0];
    s.payload[128] = 1;
    s.digest = ipc::digest(std::span(s.payload).first(s.length));
  });
  EXPECT_FALSE(reader.commit_handoff(*offered));
  EXPECT_EQ(memory->state.head, 0u);
  EXPECT_EQ(reader.snapshot().error, ipc::reader_error::invalid_protocol);
}

TEST_F(FileAudioIPC, ProducerCloseBetweenPeekAndCommit) {
  append();
  ipc::reader reader;
  ASSERT_TRUE(reader.open(identity));
  auto offered = reader.peek_record();
  ASSERT_TRUE(offered);
  locked([&] {
    memory->state.producer_closed = 1;
  });
  EXPECT_FALSE(reader.commit_handoff(*offered));
  EXPECT_EQ(memory->state.head, 0u);
  EXPECT_EQ(memory->state.fault, 0u);
  EXPECT_EQ(reader.snapshot().error, ipc::reader_error::producer_closed);
  EXPECT_TRUE(reader.retire());
}

TEST_F(FileAudioIPC, ProducerTailRewindRejected) {
  append();
  append();
  ipc::reader reader;
  ASSERT_TRUE(reader.open(identity));
  ASSERT_TRUE(reader.peek_record());
  locked([&] {
    --memory->state.tail;
    --memory->state.produced_records;
    --memory->state.produced_frames;
  });
  EXPECT_FALSE(reader.peek_record());
  EXPECT_EQ(reader.snapshot().error, ipc::reader_error::invalid_protocol);
}

TEST_F(FileAudioIPC, OwnedCounterMutationRejected) {
  append();
  ipc::reader reader;
  ASSERT_TRUE(reader.open(identity));
  locked([&] {
    ++memory->state.head;
    ++memory->state.handed_records;
    ++memory->state.handed_frames;
  });
  EXPECT_FALSE(reader.peek_record());
  EXPECT_EQ(reader.snapshot().error, ipc::reader_error::invalid_protocol);
}

TEST_F(FileAudioIPC, ChangedScopeCannotCommitOrRetireAsOriginal) {
  append();
  ipc::reader reader;
  ASSERT_TRUE(reader.open(identity));
  auto offered = reader.peek_record();
  ASSERT_TRUE(offered);
  locked([&] {
    ++memory->state.epoch;
  });
  EXPECT_FALSE(reader.commit_handoff(*offered));
  EXPECT_EQ(memory->state.head, 0u);
  EXPECT_FALSE(reader.retire());
  EXPECT_FALSE(reader.snapshot().resources_released);
}

TEST_F(FileAudioIPC, BusyMutexRetirementRetainsResourcesAndRetries) {
  ipc::reader reader;
  ASSERT_TRUE(reader.open(identity));
  std::promise<void> acquired, release;
  auto resume = release.get_future();
  std::thread worker([&] {
    EXPECT_EQ(WaitForSingleObject(mutex, 1000), DWORD(WAIT_OBJECT_0));
    acquired.set_value();
    resume.wait();
    EXPECT_TRUE(ReleaseMutex(mutex));
  });
  acquired.get_future().wait();
  EXPECT_FALSE(reader.peek_record());
  EXPECT_TRUE(reader.snapshot().accepting);
  EXPECT_FALSE(reader.retire());
  EXPECT_TRUE(reader.snapshot().opened);
  EXPECT_FALSE(reader.snapshot().resources_released);
  EXPECT_FALSE(reader.snapshot().accepting);
  release.set_value();
  worker.join();
  EXPECT_TRUE(reader.retire());
  EXPECT_EQ(memory->state.consumer_closed, 1u);
}

TEST_F(FileAudioIPC, AbandonedRealTransactionNeverCommits) {
  append();
  ipc::reader reader;
  ASSERT_TRUE(reader.open(identity));
  auto offered = reader.peek_record();
  ASSERT_TRUE(offered);
  std::thread abandon([&] {
    EXPECT_EQ(WaitForSingleObject(mutex, 1000), DWORD(WAIT_OBJECT_0));
  });
  abandon.join();
  EXPECT_FALSE(reader.commit_handoff(*offered));
  EXPECT_EQ(memory->state.head, 0u);
  EXPECT_EQ(memory->state.fault, 4u);
  EXPECT_EQ(reader.snapshot().error, ipc::reader_error::abandoned_mutex);
}

TEST_F(FileAudioIPC, ProducerBirthMismatchAndFailedOpenCleanup) {
  locked([&] {
    ++memory->state.producer_creation;
  });
  ipc::reader reader;
  EXPECT_FALSE(reader.open(identity));
  EXPECT_EQ(reader.snapshot().error, ipc::reader_error::producer_lost);
  EXPECT_FALSE(reader.retire());
  EXPECT_TRUE(reader.snapshot().resources_released);
  EXPECT_EQ(memory->state.consumer_attached, 0u);
}

TEST_F(FileAudioIPC, InvalidScopeAndOneOpenAttempt) {
  auto invalid = identity;
  invalid.channel.fill(0);
  ipc::reader reader;
  EXPECT_FALSE(reader.open(invalid));
  EXPECT_FALSE(reader.open(identity));
  EXPECT_EQ(reader.snapshot().error, ipc::reader_error::invalid_scope);
}

TEST_F(FileAudioIPC, MissingObjectsAndUndersizedViewFailClosed) {
  auto absent = identity;
  absent.channel[15] ^= 0x80;
  ipc::reader missing_mutex;
  EXPECT_FALSE(missing_mutex.open(absent));
  EXPECT_EQ(missing_mutex.snapshot().error, ipc::reader_error::open_mutex);
  EXPECT_TRUE(UnmapViewOfFile(memory));
  memory = nullptr;
  EXPECT_TRUE(CloseHandle(mapping));
  mapping = nullptr;
  ipc::reader missing_mapping;
  EXPECT_FALSE(missing_mapping.open(identity));
  EXPECT_EQ(missing_mapping.snapshot().error, ipc::reader_error::open_mapping);
  mapping = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0, 64, name.c_str());
  ASSERT_NE(mapping, nullptr);
  ipc::reader undersized;
  EXPECT_FALSE(undersized.open(identity));
  EXPECT_EQ(undersized.snapshot().error, ipc::reader_error::map_view);
}

TEST_F(FileAudioIPC, OpenAbandonedOrInvalidMappingNeverAttaches) {
  std::thread abandon([&] {
    EXPECT_EQ(WaitForSingleObject(mutex, 1000), DWORD(WAIT_OBJECT_0));
  });
  abandon.join();
  ipc::reader abandoned;
  EXPECT_FALSE(abandoned.open(identity));
  EXPECT_EQ(abandoned.snapshot().error, ipc::reader_error::abandoned_mutex);
  EXPECT_EQ(memory->state.consumer_attached, 0u);
  EXPECT_FALSE(abandoned.retire());
  locked([&] {
    memory->state.fault = 0;
    memory->state.version = 9;
  });
  ipc::reader invalid;
  EXPECT_FALSE(invalid.open(identity));
  EXPECT_EQ(invalid.snapshot().error, ipc::reader_error::invalid_protocol);
  EXPECT_EQ(memory->state.consumer_attached, 0u);
}

TEST_F(FileAudioIPC, RemoteFaultAndStopWithoutOpen) {
  ipc::reader absent;
  EXPECT_FALSE(absent.stop());
  ipc::reader reader;
  ASSERT_TRUE(reader.open(identity));
  locked([&] {
    memory->state.fault = 3;
  });
  EXPECT_FALSE(reader.peek_record());
  EXPECT_EQ(reader.snapshot().error, ipc::reader_error::remote_fault);
  EXPECT_TRUE(reader.stop());
  EXPECT_TRUE(reader.stop());
  EXPECT_TRUE(reader.retire());
}

TEST_F(FileAudioIPC, HandoffMutexTimeoutClosesGateAndPreservesCursor) {
  append();
  ipc::reader reader;
  ASSERT_TRUE(reader.open(identity));
  auto offered = reader.peek_record();
  ASSERT_TRUE(offered);
  std::promise<void> acquired, release;
  auto resume = release.get_future();
  std::thread worker([&] {
    EXPECT_EQ(WaitForSingleObject(mutex, 1000), DWORD(WAIT_OBJECT_0));
    acquired.set_value();
    resume.wait();
    EXPECT_TRUE(ReleaseMutex(mutex));
  });
  acquired.get_future().wait();
  EXPECT_FALSE(reader.commit_handoff(*offered));
  EXPECT_FALSE(reader.snapshot().accepting);
  EXPECT_EQ(reader.snapshot().error, ipc::reader_error::mutex_timeout);
  EXPECT_EQ(reader.snapshot().windows_error, DWORD(ERROR_TIMEOUT));
  release.set_value();
  worker.join();
  EXPECT_EQ(memory->state.head, 0u);
  EXPECT_TRUE(reader.retire());
}

TEST_F(FileAudioIPC, ExactLayoutWireAndCorruptionValidation) {
  EXPECT_TRUE(ipc::valid(*memory, identity));
  append();
  const auto original = *memory;
  auto changed = original;
  changed.records[0].payload[128] = 1;
  EXPECT_FALSE(ipc::valid(changed, identity));
  changed = original;
  changed.records[0].payload[200] = 1;
  EXPECT_FALSE(ipc::valid(changed, identity));
  changed = original;
  changed.records[0].length = 5000;
  EXPECT_FALSE(ipc::valid(changed, identity));
  changed = original;
  ++changed.records[0].sequence;
  EXPECT_FALSE(ipc::valid(changed, identity));
  changed = original;
  changed.state.reserved[55] = 1;
  EXPECT_FALSE(ipc::valid(changed, identity));
  changed = original;
  ++changed.state.produced_frames;
  EXPECT_FALSE(ipc::valid(changed, identity));
  changed = original;
  changed.state.eof_produced = 1;
  EXPECT_FALSE(ipc::valid(changed, identity));
  changed = original;
  changed.records[0].reserved_word = 1;
  EXPECT_FALSE(ipc::valid(changed, identity));
  changed = original;
  changed.records[0].payload[16] ^= 1;
  EXPECT_FALSE(ipc::valid(changed, identity));
  changed = original;
  changed.records[0].payload[80] ^= 1;
  EXPECT_FALSE(ipc::valid(changed, identity));
  changed = original;
  changed.state.tail = 9;
  EXPECT_FALSE(ipc::valid(changed, identity));
  changed = original;
  changed.state.consumer_attached = 1;
  EXPECT_FALSE(ipc::valid(changed, identity));
}

TEST_F(FileAudioIPC, EmptyEOFForgeryAndTrailingRecordsRejected) {
  auto changed = *memory;
  changed.state.eof_produced = changed.state.eof_handed = 1;
  EXPECT_FALSE(ipc::valid(changed, identity));
  append(true);
  EXPECT_TRUE(ipc::valid(*memory, identity));
  append();
  EXPECT_FALSE(ipc::valid(*memory, identity));
}

/** @brief Run only this private production-reader fixture. */
int main(int argc, char **argv) {
  testing::InitGoogleTest(&argc, argv);
  return RUN_ALL_TESTS();
}
