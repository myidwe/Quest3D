// Actual production backend + miniaudio Null. Dispatch/start errors below are
// explicit fixtures, not measurements of AAudio/Quest speakers or a DAC.
#include "audio/miniaudio_backend.h"
#include <miniaudio.h>
#include <atomic>
#include <cassert>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <thread>
#include <type_traits>

using namespace nightfall;
static_assert(!std::is_constructible<BackendStartupWatch, AudioStartupIdentity, AudioStartupState>::value);
static_assert(!std::is_constructible<BackendStartupSnapshot, BackendStartupFacts>::value);
static_assert(!std::is_default_constructible<AudioStartupWatch>::value);

template<class Test> void until(Test test) {
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(3);
    while (!test()) { assert(std::chrono::steady_clock::now() < deadline); std::this_thread::yield(); }
}

class ProbeBackend : public MiniaudioBackend {
    static ProbeBackend *active;
    ma_device_data_proc original_data = nullptr;
    static void dispatch(ma_device *device, void *output, const void *input, ma_uint32 frames) {
        auto *self = active;
        assert(self && self->device == device);
        ++self->dispatches;
        if (self->suppress.load()) {
            std::memset(output, 0, size_t(frames) * device->playback.channels * sizeof(float));
        } else self->original_data(device, output, input, frames);
    }
protected:
    int init_device(ma_device *value, int rate, int channels, int frames) override {
        const auto result = MiniaudioBackend::init_device(value, rate, channels, frames);
        if (result == MA_SUCCESS) {
            device = value;
            original_data = value->onData;
            value->onData = dispatch;
        }
        return result;
    }
    int start_device(ma_device *value) override {
        if (fail_start_before) return MA_ERROR;
        const int result = MiniaudioBackend::start_device(value);
        if (hold_start) {
            start_waiting = true;
            until([&] { return release_start.load(); });
        }
        return fail_start_after ? MA_ERROR : result;
    }
    int stop_device(ma_device *value) override {
        const int result = MiniaudioBackend::stop_device(value);
        return fail_stop ? MA_ERROR : result;
    }
    void callback_entered() override {
        if (hold_callback.exchange(false)) {
            callback_waiting = true;
            until([&] { return release_callback.load(); });
        }
    }
public:
    ma_device *device = nullptr;
    std::atomic<uint64_t> dispatches{0};
    std::atomic<bool> suppress{false}, hold_callback{false}, callback_waiting{false}, release_callback{false};
    std::atomic<bool> hold_start{false}, start_waiting{false}, release_start{false};
    bool fail_start_before = false, fail_start_after = false, fail_stop = false;
    ProbeBackend() { assert(!active); active = this; }
    ~ProbeBackend() override { release_callback = true; release_start = true; shutdown(); active = nullptr; }
    void delivered_without_production_callback() {
        const auto before = dispatches.load();
        until([&] { return dispatches.load() >= before + 3; });
    }
    void notify(ma_device_notification_type type) {
        ma_device_notification notification{};
        notification.pDevice = device;
        notification.type = type;
        assert(device->onNotification);
        device->onNotification(&notification);
    }
};
ProbeBackend *ProbeBackend::active = nullptr;

static auto ready(ProbeBackend &backend) {
    auto watch = backend.startup_watch();
    assert(watch);
    until([&] { return watch->snapshot().ready(); });
    assert(watch->snapshot().facts().start_returned_success);
    return watch;
}

int main() {
    std::shared_ptr<const BackendStartupWatch> old_object;
    {
        ProbeBackend backend;
        assert(!backend.startup_watch());
        backend.suppress = true;
        assert(backend.initialize(48000, 2, 240));
        assert(backend.get_backend_name() == "Null");
        auto initial = backend.startup_watch();
        backend.delivered_without_production_callback();
        assert(!initial->snapshot().ready());
        assert(initial->snapshot().facts().state == AudioStartupState::awaiting_callback);
        assert(initial->snapshot().facts().callbacks_entered_after_start == 0);
        backend.suppress = false;
        initial = ready(backend); // Natural empty-ring silence, no PCM warmup.
        const auto historical = initial->snapshot();
        assert(historical.ready());
        backend.pause();
        assert(!initial->snapshot().ready() && initial->snapshot().facts().state == AudioStartupState::paused);
        assert(historical.ready()); // Snapshot is history; coordinator must poll its watch.
        backend.suppress = true;
        backend.resume();
        auto resumed = backend.startup_watch();
        assert(resumed->identity().lifetime == initial->identity().lifetime);
        assert(resumed->identity().attempt > initial->identity().attempt);
        backend.delivered_without_production_callback();
        assert(!resumed->snapshot().ready() && !initial->snapshot().ready());
        backend.suppress = false;
        ready(backend);

        backend.pause();
        backend.hold_start = true;
        std::thread start([&] { backend.resume(); });
        until([&] { return backend.start_waiting.load(); });
        auto before_return = backend.startup_watch();
        backend.delivered_without_production_callback(); // Actual callbacks enter while start wrapper is held.
        assert(!before_return->snapshot().ready());
        assert(!before_return->snapshot().facts().start_returned_success);
        assert(before_return->snapshot().facts().callbacks_entered_after_start == 0);
        backend.release_start = true;
        start.join();
        ready(backend);
        backend.hold_start = false;

        // An old in-flight production callback cannot satisfy the next start.
        auto old_attempt = backend.startup_watch();
        const auto old_returns = old_attempt->snapshot().facts().callbacks_returned_after_start;
        backend.hold_callback = true;
        until([&] { return backend.callback_waiting.load(); });
        backend.suppress = true;
        backend.resume(); // Real miniaudio already-started fast path.
        auto replacement = backend.startup_watch();
        assert(replacement->identity().attempt > old_attempt->identity().attempt);
        assert(!replacement->snapshot().ready() && !old_attempt->snapshot().ready());
        backend.release_callback = true;
        until([&] { return old_attempt->snapshot().facts().callbacks_returned_after_start > old_returns; });
        backend.delivered_without_production_callback();
        assert(!replacement->snapshot().ready());
        assert(replacement->snapshot().facts().callbacks_entered_after_start == 0);
        backend.suppress = false;
        ready(backend);

        // Real device stop outside the owner generates a production notification.
        auto interrupted = backend.startup_watch();
        assert(ma_device_stop(backend.device) == MA_SUCCESS);
        assert(!interrupted->snapshot().ready());
        assert(interrupted->snapshot().facts().state == AudioStartupState::disconnected);
        backend.notify(ma_device_notification_type_started);
        backend.resume();
        backend.delivered_without_production_callback();
        assert(!backend.startup_watch()->snapshot().ready()); // Fault sticks through this init generation.
        const auto closed_lifetime = backend.lifetime();
        assert(backend.shutdown_observed(closed_lifetime).resources_released());
        assert(backend.initialize(48000, 2, 240));
        auto fresh = ready(backend);
        assert(fresh->identity().lifetime.generation > interrupted->identity().lifetime.generation);
        assert(!interrupted->snapshot().ready());
        old_object = fresh;
    }
    assert(!old_object->snapshot().ready());

    {
        ProbeBackend backend;
        backend.fail_start_before = true;
        assert(!backend.initialize(48000, 2, 240));
        auto failed = backend.startup_watch();
        assert(failed->snapshot().facts().state == AudioStartupState::failed);
        assert(!failed->snapshot().ready() && failed->snapshot().facts().first_error == MA_ERROR);
        backend.fail_start_before = false;
        assert(backend.initialize(48000, 2, 240));
        auto next = ready(backend);
        assert(next->identity().lifetime.instance != old_object->identity().lifetime.instance);
        backend.pause();
        backend.hold_start = true;
        backend.fail_start_after = true;
        std::thread start([&] { backend.resume(); });
        until([&] { return backend.start_waiting.load(); });
        auto after_callback_failure = backend.startup_watch();
        backend.delivered_without_production_callback();
        assert(!after_callback_failure->snapshot().ready());
        backend.release_start = true;
        start.join();
        backend.hold_start = false;
        backend.fail_start_after = false;
        assert(after_callback_failure->snapshot().facts().first_error == MA_ERROR);
        backend.resume();
        backend.delivered_without_production_callback();
        assert(!backend.startup_watch()->snapshot().ready());
        assert(!after_callback_failure->snapshot().ready());
    }

    for (auto event : {ma_device_notification_type_rerouted, ma_device_notification_type_interruption_began}) {
        ProbeBackend backend;
        assert(backend.initialize(48000, 2, 240));
        auto watch = ready(backend);
        backend.notify(event); // Explicit notification fault, not a real hardware reroute.
        backend.notify(ma_device_notification_type_started);
        backend.delivered_without_production_callback();
        assert(watch->snapshot().facts().state == AudioStartupState::disconnected && !watch->snapshot().ready());
    }
    {
        ProbeBackend backend;
        assert(backend.initialize(48000, 2, 240));
        auto watch = ready(backend);
        miniaudio_device_error(backend.device, -899); // Real observer, injected AAudio-shaped error.
        backend.notify(ma_device_notification_type_started);
        backend.delivered_without_production_callback();
        assert(watch->snapshot().facts().first_error == -899 && !watch->snapshot().ready());
        backend.resume();
        assert(backend.startup_watch()->snapshot().facts().first_error == -899);
        assert(!backend.startup_watch()->snapshot().ready());
    }
    {
        ProbeBackend backend;
        assert(backend.initialize(48000, 2, 240));
        auto watch = ready(backend);
        backend.fail_stop = true;
        backend.pause();
        assert(watch->snapshot().facts().first_error == MA_ERROR && !watch->snapshot().ready());
        backend.fail_stop = false;
        backend.resume();
        assert(!backend.startup_watch()->snapshot().ready());
    }
    for (int cycle = 0; cycle < 20; ++cycle) {
        ProbeBackend backend;
        assert(backend.initialize(48000, 2, 240));
        auto watch = ready(backend);
        backend.hold_callback = true;
        until([&] { return backend.callback_waiting.load(); });
        std::atomic<bool> closed{false};
        std::thread shutdown([&] {
            assert(backend.shutdown_observed(watch->identity().lifetime).clean_active_shutdown());
            closed = true;
        });
        until([&] { return watch->snapshot().facts().state == AudioStartupState::closed; });
        assert(!closed && !watch->snapshot().ready());
        backend.release_callback = true;
        shutdown.join();
        assert(closed && !watch->snapshot().ready());
    }
    std::puts("PASS actual miniaudio Null startup: successful start return + post-return production callback, no PCM warmup, missing callback, pre-return callback, failed start after callbacks, old attempt held across resume, real unexpected stop, injected reroute/interruption/raw error, sticky failure, scope replacement, 20 held-callback shutdown races");
}
