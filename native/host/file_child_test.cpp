/** @file native/host/file_child_test.cpp
 * @brief Actual Windows child/job/private-pipe fixtures, using the workspace
 * Python redirector.
 */
#include "src/platform/windows/quest3d_file_child.h"

#include <fstream>
#include <gtest/gtest.h>
#include <sstream>

namespace child = quest3d::file_child;

/** @brief One unique create-new stderr path per real child lifetime. */
class FileChild : public testing::Test {
protected:
  /** @brief Configure a fixed interpreter/script vector; no shell is invoked.
   */
  child::launch_options options(const std::wstring &mode) {
    static unsigned sequence = 0;
    child::launch_options value;
    value.interpreter = FILE_CHILD_PYTHON;
    value.working_directory = std::filesystem::current_path();
    value.stderr_file = value.working_directory /
                        (L"child-" + std::to_wstring(GetCurrentProcessId()) +
                         L"-" + std::to_wstring(++sequence) + L".stderr");
    value.arguments = {L"-u", FILE_CHILD_FIXTURE, mode};
    return value;
  }

  /** @brief Wait for an actual line with bounded polls, never promoting elapsed
   * time to data. */
  child::io_result read(child::process &value) {
    child::io_result result;
    const auto deadline = GetTickCount64() + 5000;
    do {
      result = value.read_message(100);
    } while (result.state == child::io_state::pending &&
             GetTickCount64() < deadline);
    return result;
  }

  /** @brief Check the real hello worker belongs to this private job, including
   * venv redirector descendants. */
  void hello(child::process &value) {
    const auto message = read(value);
    ASSERT_EQ(message.state, child::io_state::message);
    unsigned long pid = 0;
    unsigned long long created = 0;
    ASSERT_EQ(std::sscanf(message.bytes.c_str(),
                          "{\"pid\":%lu,\"creation\":%llu}", &pid, &created),
              2);
    ASSERT_TRUE(value.retain_worker(pid, created));
    EXPECT_EQ(value.poll_exit().worker_id, pid);
    EXPECT_EQ(value.poll_exit().worker_creation, created);
    ASSERT_EQ(value.write_message("{\"go\":true}", 1000).state,
              child::io_state::sent);
  }
};

TEST_F(FileChild, EchoAndStderrAreSeparateAndWorkerHasRealJobIdentity) {
  auto config = options(L"echo");
  child::process value;
  ASSERT_TRUE(value.launch(config));
  hello(value);
  EXPECT_EQ(value.write_message("{\"hello\":\"world\"}", 1000).state,
            child::io_state::sent);
  const auto reply = read(value);
  EXPECT_EQ(reply.state, child::io_state::message);
  EXPECT_EQ(reply.bytes, "{\"hello\":\"world\"}");
  EXPECT_FALSE(value.retire(0));
  EXPECT_FALSE(value.poll_exit().resources_released);
  ASSERT_TRUE(value.request_stop());
  ASSERT_TRUE(value.retire(5000));
  const auto facts = value.poll_exit();
  EXPECT_TRUE(facts.process_exited);
  EXPECT_TRUE(facts.job_empty);
  EXPECT_TRUE(facts.resources_released);
  std::ifstream file(config.stderr_file);
  std::string text((std::istreambuf_iterator<char>(file)), {});
  EXPECT_NE(text.find("fixture stderr only"), std::string::npos);
  EXPECT_EQ(text.find("hello"), std::string::npos);
}

TEST_F(FileChild, PartialAndCoalescedLinesResumeWithoutLoss) {
  child::process value;
  ASSERT_TRUE(value.launch(options(L"partial")));
  hello(value);
  EXPECT_EQ(value.read_message(30).state, child::io_state::pending);
  EXPECT_GT(value.poll_exit().buffered_read_bytes, 0u);
  EXPECT_EQ(value.write_message("{}", 1000).state, child::io_state::sent);
  EXPECT_EQ(read(value).bytes, "{\"partial\":true}");
  EXPECT_EQ(read(value).bytes, "{\"coalesced\":1}");
  EXPECT_EQ(read(value).state, child::io_state::eof);
  EXPECT_TRUE(value.retire(5000));
  EXPECT_FALSE(value.poll_exit().termination_requested);
}

TEST_F(FileChild, CRAndOversizedMessagesNeverReturnAsValidLines) {
  for (const auto &mode : {L"cr", L"oversize"}) {
    child::process value;
    ASSERT_TRUE(value.launch(options(mode)));
    hello(value);
    EXPECT_EQ(read(value).state, child::io_state::invalid);
    EXPECT_LE(value.poll_exit().buffered_read_bytes, 8193u);
    EXPECT_TRUE(value.request_stop());
    EXPECT_TRUE(value.retire(5000));
  }
}

TEST_F(FileChild, TruncatedEOFIsFailureAndCleanProcessExitIsStillObserved) {
  child::process value;
  ASSERT_TRUE(value.launch(options(L"truncated")));
  hello(value);
  EXPECT_EQ(read(value).state, child::io_state::failed);
  EXPECT_EQ(value.poll_exit().first_error, child::error::truncated_line);
  EXPECT_TRUE(value.retire(5000));
  EXPECT_TRUE(value.poll_exit().process_exited);
}

TEST_F(FileChild, PendingReadIsActuallyCancelledBeforeRetirement) {
  child::process value;
  ASSERT_TRUE(value.launch(options(L"sleep")));
  hello(value);
  EXPECT_EQ(value.read_message(0).state, child::io_state::pending);
  EXPECT_TRUE(value.poll_exit().read_pending);
  EXPECT_TRUE(value.request_stop());
  EXPECT_TRUE(value.retire(5000));
  EXPECT_FALSE(value.poll_exit().read_pending);
  EXPECT_TRUE(value.poll_exit().termination_requested);
}

TEST_F(FileChild, BackpressuredWriteRetainsOneExactMessageAndCancels) {
  child::process value;
  ASSERT_TRUE(value.launch(options(L"sleep")));
  hello(value);
  const std::string large = std::string(8192, 'x');
  child::io_result result;
  for (int i = 0; i < 32; ++i) {
    result = value.write_message(large, 0);
    if (result.state == child::io_state::pending) {
      break;
    }
  }
  ASSERT_EQ(result.state, child::io_state::pending);
  EXPECT_TRUE(value.poll_exit().write_pending);
  EXPECT_LE(value.poll_exit().pending_write_bytes, 8193u);
  EXPECT_EQ(value.write_message("{}", 0).state, child::io_state::invalid);
  EXPECT_EQ(value.write_message(large, 0).state, child::io_state::pending);
  EXPECT_TRUE(value.request_stop());
  EXPECT_TRUE(value.retire(5000));
  EXPECT_FALSE(value.poll_exit().write_pending);
}

TEST_F(FileChild,
       ArgumentVectorQuotingIncludesEmptySpaceQuoteAndTrailingSlash) {
  auto config = options(L"argv");
  config.arguments.insert(config.arguments.end(),
                          {L"", L"a b", L"x\"y\\", L"$(echo unsafe)", L"a&b"});
  child::process value;
  ASSERT_TRUE(value.launch(config));
  hello(value);
  EXPECT_EQ(read(value).bytes,
            "[\"\",\"a b\",\"x\\\"y\\\\\",\"$(echo unsafe)\",\"a&b\"]");
  EXPECT_TRUE(value.retire(5000));
}

TEST_F(FileChild, OriginalExitCannotRetireAnAliveDescendant) {
  child::process value;
  ASSERT_TRUE(value.launch(options(L"tree")));
  hello(value);
  auto descendant = read(value);
  ASSERT_EQ(descendant.state, child::io_state::message);
  unsigned long pid = 0;
  unsigned long long birth = 0;
  ASSERT_EQ(std::sscanf(descendant.bytes.c_str(),
                        "{\"pid\":%lu,\"creation\":%llu}", &pid, &birth),
            2);
  HANDLE retained =
      OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
  ASSERT_NE(retained, nullptr);
  EXPECT_FALSE(value.retire(200));
  EXPECT_FALSE(value.poll_exit().job_empty);
  EXPECT_FALSE(value.poll_exit().resources_released);
  EXPECT_TRUE(value.request_stop());
  EXPECT_TRUE(value.retire(5000));
  EXPECT_EQ(WaitForSingleObject(retained, 0), DWORD(WAIT_OBJECT_0));
  EXPECT_TRUE(CloseHandle(retained));
}

TEST_F(FileChild, UnrelatedPIDCannotBecomeTheWorker) {
  child::process value;
  ASSERT_TRUE(value.launch(options(L"sleep")));
  ASSERT_EQ(read(value).state, child::io_state::message);
  FILETIME c{}, e{}, k{}, u{};
  ASSERT_TRUE(GetProcessTimes(GetCurrentProcess(), &c, &e, &k, &u));
  const auto birth = (std::uint64_t(c.dwHighDateTime) << 32) | c.dwLowDateTime;
  EXPECT_FALSE(value.retain_worker(GetCurrentProcessId(), birth));
  EXPECT_EQ(value.poll_exit().first_error, child::error::worker_identity);
  EXPECT_TRUE(value.request_stop());
  EXPECT_TRUE(value.retire(5000));
}

TEST_F(FileChild, InvalidLaunchAndCreateNewStderrPreserveExistingFiles) {
  auto invalid = options(L"echo");
  invalid.interpreter = L"relative-python.exe";
  child::process bad;
  EXPECT_FALSE(bad.launch(invalid));
  EXPECT_EQ(bad.poll_exit().first_error, child::error::invalid_options);
  EXPECT_TRUE(bad.retire());
  auto config = options(L"echo");
  {
    std::ofstream file(config.stderr_file);
    file << "preserve";
  }
  child::process existing;
  EXPECT_FALSE(existing.launch(config));
  EXPECT_EQ(existing.poll_exit().first_error, child::error::stderr_open);
  EXPECT_TRUE(existing.retire());
  std::ifstream file(config.stderr_file);
  std::string text;
  file >> text;
  EXPECT_EQ(text, "preserve");
}

TEST_F(FileChild, ExactBoundedLineAndInvalidOutgoingFraming) {
  child::process value;
  EXPECT_EQ(value.read_message().state, child::io_state::closed);
  EXPECT_EQ(value.write_message("{}").state, child::io_state::closed);
  EXPECT_FALSE(value.retain_worker(1, 1));
  EXPECT_TRUE(value.request_stop());
  ASSERT_TRUE(value.launch(options(L"echo")));
  hello(value);
  EXPECT_FALSE(value.launch(options(L"echo")));
  EXPECT_FALSE(value.retain_worker(value.poll_exit().worker_id,
                                   value.poll_exit().worker_creation));
  for (const auto &line :
       {std::string(8193, 'x'), std::string("a\nb"), std::string("a\rb")}) {
    EXPECT_EQ(value.write_message(line).state, child::io_state::invalid);
  }
  const std::string exact(8192, 'x');
  auto written = value.write_message(exact, 1000);
  ASSERT_EQ(written.state, child::io_state::sent);
  EXPECT_EQ(read(value).bytes, exact);
  EXPECT_EQ(value.write_message("", 1000).state, child::io_state::sent);
  const auto empty = read(value);
  EXPECT_EQ(empty.state, child::io_state::message);
  EXPECT_TRUE(empty.bytes.empty());
  EXPECT_TRUE(value.request_stop());
  EXPECT_TRUE(value.retire(5000));
  EXPECT_EQ(value.read_message().state, child::io_state::closed);
  EXPECT_TRUE(value.retire());
  EXPECT_TRUE(value.request_stop());
}

TEST_F(FileChild, ActualFileWorkerOpenDescribeCloseUsesOwnedPrivateProcess) {
  const std::wstring nonce = L"11111111111111111111111111111111";
  auto config = options(L"unused");
  config.arguments = {
      L"-u",
      L"-m",
      L"quest3d.file_worker",
      L"--worker-nonce",
      nonce,
      L"--transport",
      L"22222222222222222222222222222222",
      L"--output",
      (config.working_directory / "actual-worker-output").native()};
  child::process value;
  ASSERT_TRUE(value.launch(config));
  std::ofstream transcript("actual-worker-transcript.jsonl", std::ios::binary);
  const auto initial = read(value);
  ASSERT_EQ(initial.state, child::io_state::message) << initial.bytes;
  transcript << initial.bytes << '\n';
  char actual_nonce[33]{};
  unsigned long pid = 0;
  unsigned long long birth = 0;
  int consumed = 0;
  ASSERT_EQ(std::sscanf(initial.bytes.c_str(),
                        "{\"v\":1,\"type\":\"hello\",\"worker_nonce\":\"%32[0-"
                        "9a-f]\",\"pid\":%lu,\"creation_filetime\":\"%llu\"}%n",
                        actual_nonce, &pid, &birth, &consumed),
            3);
  ASSERT_EQ(std::size_t(consumed), initial.bytes.size());
  ASSERT_EQ(std::string(actual_nonce), "11111111111111111111111111111111");
  ASSERT_TRUE(value.retain_worker(pid, birth));
  const auto bound = value.poll_exit();
  unsigned sequence = 0;
  auto request = [&](const std::string &op, const std::string &data) {
    const auto seq = std::to_string(++sequence);
    const auto wire = "{\"v\":1,\"worker_nonce\":"
                      "\"11111111111111111111111111111111\",\"seq\":\"" +
                      seq + "\",\"op\":\"" + op + "\",\"data\":" + data + "}";
    EXPECT_EQ(value.write_message(wire, 1000).state, child::io_state::sent);
    const auto result = read(value);
    EXPECT_EQ(result.state, child::io_state::message) << result.bytes;
    EXPECT_NE(result.bytes.find("\"seq\":\"" + seq + "\""), std::string::npos);
    EXPECT_NE(result.bytes.find("\"type\":\"reply\""), std::string::npos);
    EXPECT_NE(result.bytes.find("\"ok\":true"), std::string::npos)
        << result.bytes;
    transcript << result.bytes << '\n';
    return result.bytes;
  };
  // This known fixture path uses generic forward slashes and has no JSON
  // metacharacters.
  const auto path =
      (std::filesystem::path(FILE_CHILD_ROOT) /
       "artifacts/audio/file-audio-vectors-20260910-a/known-av.nut")
          .generic_string();
  request("open", "{\"path\":\"" + path + "\",\"mode\":\"2d\"}");
  std::string description;
  const auto deadline = GetTickCount64() + 5000;
  do {
    description = request("describe", "{}");
    if (description.find("\"metadata_ready\":true") != std::string::npos &&
        description.find("\"preroll_ready\":true") != std::string::npos) {
      break;
    }
    Sleep(10); // Only schedules the next actual decoder status query.
  } while (GetTickCount64() < deadline);
  EXPECT_NE(description.find("\"metadata_ready\":true"), std::string::npos);
  EXPECT_NE(description.find("\"preroll_ready\":true"), std::string::npos);
  EXPECT_NE(description.find("\"has_audio\":true"), std::string::npos);
  EXPECT_NE(description.find("\"audio_frames_consumed\":0"), std::string::npos);
  EXPECT_NE(description.find("\"video_publication_active\":false"),
            std::string::npos);
  EXPECT_NE(description.find("\"quest_ready_verified\":false"),
            std::string::npos);
  const auto closed = request("close", "{}");
  EXPECT_NE(closed.find("\"closed\":true"), std::string::npos);
  EXPECT_EQ(read(value).state, child::io_state::eof);
  ASSERT_TRUE(value.retire(5000));
  const auto retired = value.poll_exit();
  EXPECT_TRUE(retired.process_exited && retired.worker_exited &&
              retired.job_empty && retired.resources_released);
  EXPECT_FALSE(retired.termination_requested || retired.read_pending ||
               retired.write_pending);
  EXPECT_EQ(retired.exit_code, 0u);
  EXPECT_EQ(retired.first_error, child::error::none);
  std::ofstream facts("actual-worker-lifetime.json");
  facts << "{\"original_pid\":" << bound.process_id
        << ",\"original_creation\":\"" << bound.process_creation
        << "\",\"worker_pid\":" << bound.worker_id << ",\"worker_creation\":\""
        << bound.worker_creation
        << "\",\"process_exited\":" << retired.process_exited
        << ",\"worker_exited\":" << retired.worker_exited
        << ",\"job_empty\":" << retired.job_empty
        << ",\"resources_released\":" << retired.resources_released
        << ",\"exit_code\":" << retired.exit_code
        << ",\"authenticated_host_coordinator\":false}\n";
}

/** @brief Run only the test-owned native child lifetimes. */
int main(int argc, char **argv) {
  testing::InitGoogleTest(&argc, argv);
  return RUN_ALL_TESTS();
}
