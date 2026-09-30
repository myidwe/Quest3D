#pragma once
#include <atomic>
namespace fixture {
enum Mode { normal, no_audio_failure, opus_failure, cancelled_start, duplicate_audio };
extern std::atomic<int> mode;
extern std::atomic<int> duplicate_init_result;
extern std::atomic<bool> check_fixture_host;
extern std::atomic<bool> entered, ready, interrupted, stop_entered, hold_stop, drain_failure;
void reset(Mode selected);
void terminate_current(int error);
}
