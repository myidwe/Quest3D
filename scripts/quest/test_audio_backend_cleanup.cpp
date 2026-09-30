// Production MiniaudioBackend compiled with only miniaudio's null backend.
// No OS audio device opens and no audible output; fault cases are explicit.
#include "audio/miniaudio_backend.h"
#include <miniaudio.h>
#include <atomic>
#include <cassert>
#include <chrono>
#include <cstdio>
#include <thread>
#include <vector>
#include <type_traits>

static_assert(!std::is_constructible<nightfall::BackendCleanupCompletion, nightfall::BackendCleanupFacts>::value,
    "Arbitrary facts cannot be promoted to production completion");
static_assert(!std::is_constructible<nightfall::AudioCleanupCompletion, nightfall::AudioCleanupFacts>::value,
    "Only the actual AudioRenderer may issue its completion");

class ProbeBackend : public nightfall::MiniaudioBackend {
public:
    int stop_calls = 0;
    bool fail_init = false, fail_start = false, fail_stop_once = false;
protected:
    int init_device(ma_device *device, int rate, int channels, int frames) override {
        if (fail_init) return MA_ERROR; // Injected before real device initialization.
        return MiniaudioBackend::init_device(device, rate, channels, frames);
    }
    int start_device(ma_device *device) override {
        if (fail_start) return MA_ERROR; // Real initialized device, start failure fixture.
        return MiniaudioBackend::start_device(device);
    }
    int stop_device(ma_device *device) override {
        ++stop_calls;
        const int actual = MiniaudioBackend::stop_device(device);
        assert(actual == MA_SUCCESS); // Real null device stopped even in fault case.
        if (fail_stop_once) { fail_stop_once = false; return MA_ERROR; }
        return actual;
    }
};

int main() {
    using namespace nightfall;
    ProbeBackend backend;
    assert(!backend.lifetime().valid());
    assert(!backend.shutdown_observed(backend.lifetime()).resources_released());
    assert(backend.initialize(48000, 2, 240));
    assert(backend.get_backend_name() == "Null");
    const auto first = backend.lifetime();
    const float silence[480] = {};
    assert(backend.write_pcm(silence, 240));
    const auto callback_deadline = std::chrono::steady_clock::now() + std::chrono::seconds(2);
    while (backend.get_latency_ms() > 0 && std::chrono::steady_clock::now() < callback_deadline) std::this_thread::yield();
    assert(backend.get_latency_ms() == 0); // Exercise a real null callback before teardown.
    backend.pause();
    assert(!backend.last_cleanup_completion().resources_released());
    auto closed = backend.shutdown_observed(first);
    assert(closed.clean_active_shutdown());
    assert(closed.facts().callbacks_started > 0 && closed.facts().callbacks_started == closed.facts().callbacks_finished);
    assert(closed.facts().callbacks_after_uninit == 0);
    const int calls = backend.stop_calls;
    const auto duplicate = backend.shutdown_observed(first);
    assert(duplicate.clean_active_shutdown() && backend.stop_calls == calls);
    assert(!backend.write_pcm(silence, 240));
    assert(backend.initialize(48000, 1, 240)); // Callback silence must use actual mono size.
    const auto second = backend.lifetime();
    assert(second.instance == first.instance && second.generation == first.generation + 1);
    assert(backend.shutdown_observed(first).facts().state == AudioCleanupState::scope_mismatch);
    assert(backend.write_pcm(silence, 240));
    assert(backend.shutdown_observed(second).clean_active_shutdown());
    ProbeBackend other;
    assert(other.initialize(48000, 2, 240));
    assert(other.lifetime().instance != first.instance);
    assert(other.shutdown_observed(second).facts().state == AudioCleanupState::scope_mismatch);
    assert(other.shutdown_observed(other.lifetime()).clean_active_shutdown());
    assert(!backend.initialize(48000, 0, 240));
    auto invalid = backend.shutdown_observed(backend.lifetime());
    assert(invalid.resources_released() && !invalid.clean_active_shutdown());
    assert(invalid.facts().init_stage == AudioInitStage::invalid_parameters && !invalid.facts().had_ring);
    backend.fail_init = true;
    assert(!backend.initialize(48000, 2, 240));
    auto init_fail = backend.shutdown_observed(backend.lifetime());
    assert(init_fail.resources_released() && !init_fail.clean_active_shutdown());
    assert(init_fail.facts().init_stage == AudioInitStage::device_init_failed && init_fail.facts().init_error == MA_ERROR);
    assert(init_fail.facts().pcm_ring_deleted && init_fail.facts().device_storage_deleted && !init_fail.facts().device_uninit_returned);
    backend.fail_init = false;
    backend.fail_start = true;
    assert(!backend.initialize(48000, 2, 240));
    auto start_fail = backend.shutdown_observed(backend.lifetime());
    assert(start_fail.resources_released() && !start_fail.clean_active_shutdown());
    assert(start_fail.facts().init_stage == AudioInitStage::device_start_failed && start_fail.facts().device_uninit_returned);
    backend.fail_start = false;
    assert(backend.initialize(48000, 2, 240));
    backend.fail_stop_once = true;
    backend.pause();
    auto stop_fail = backend.shutdown_observed(backend.lifetime());
    assert(stop_fail.resources_released() && !stop_fail.clean_active_shutdown());
    assert(stop_fail.facts().first_stop_error == MA_ERROR && stop_fail.facts().final_stop_result == MA_SUCCESS);
    assert(backend.shutdown_observed(backend.lifetime()).facts().first_stop_error == MA_ERROR);
    // Compete real writer/latency calls with device stop/uninit and ring deletion.
    for (int cycle = 0; cycle < 30; ++cycle) {
        assert(backend.initialize(48000, 2, 240));
        const auto scope = backend.lifetime();
        std::atomic<bool> began{false};
        std::atomic<bool> ended{false};
        std::thread writer([&] {
            began = true;
            while (!ended) { backend.write_pcm(silence, 240); (void)backend.get_latency_ms(); }
        });
        while (!began) std::this_thread::yield();
        auto result = backend.shutdown_observed(scope);
        ended = true;
        writer.join();
        assert(result.clean_active_shutdown());
        assert(!backend.write_pcm(silence, 240));
    }
    std::puts("PASS real production miniaudio null backend: lifecycle identity, pause is not cleanup, actual stop/uninit/callback retirement/ring delete, partial init/start fault, sticky stop fault, 30 concurrent writer shutdown cycles");
}
