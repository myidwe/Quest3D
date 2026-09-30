#include "audio/audio_renderer.h"
#include <miniaudio.h>
#include <godot_cpp/godot.hpp>
#include <godot_cpp/variant/dictionary.hpp>
#include <cassert>
#include <chrono>
#include <cstring>
#include <thread>

using namespace godot;
using namespace nightfall;
template<class Test> static void until(Test test) {
    const auto limit = std::chrono::steady_clock::now() + std::chrono::seconds(3);
    while (!test()) { assert(std::chrono::steady_clock::now() < limit); std::this_thread::yield(); }
}

class RendererFixtureBackend : public MiniaudioBackend {
    ma_device_data_proc production_data = nullptr;
    static RendererFixtureBackend *active;
    int starts = 0;
    static void dispatch(ma_device *device, void *output, const void *input, ma_uint32 frames) {
        auto *self = active;
        assert(self);
        ++self->dispatches;
        if (self->suppress) std::memset(output, 0, size_t(frames) * device->playback.channels * sizeof(float));
        else self->production_data(device, output, input, frames);
    }
protected:
    int init_device(ma_device *device, int rate, int channels, int frames) override {
        const int result = MiniaudioBackend::init_device(device, rate, channels, frames);
        if (result == MA_SUCCESS) { production_data = device->onData; device->onData = dispatch; }
        return result;
    }
    int start_device(ma_device *device) override {
        if (++starts > 1 && fail_resume) return MA_ERROR;
        return MiniaudioBackend::start_device(device);
    }
    void callback_entered() override {
        if (hold.exchange(false)) {
            entered = true;
            until([&] { return release.load(); });
        }
    }
public:
    std::atomic<bool> suppress{false}, hold{false}, entered{false}, release{false};
    std::atomic<uint64_t> dispatches{0};
    bool fail_resume = false;
    RendererFixtureBackend() { assert(!active); active = this; }
    ~RendererFixtureBackend() override { release = true; shutdown(); active = nullptr; }
};
RendererFixtureBackend *RendererFixtureBackend::active = nullptr;

class StartupTestRenderer : public AudioRenderer {
    GDCLASS(StartupTestRenderer, AudioRenderer)
protected:
    static void _bind_methods() {}
    std::unique_ptr<MiniaudioBackend> create_audio_backend() override {
        auto value = std::make_unique<RendererFixtureBackend>();
        backend = value.get();
        value->suppress = suppress_initial_callbacks;
        value->fail_resume = inject_resume_failure;
        return value;
    }
public:
    RendererFixtureBackend *backend = nullptr;
    bool suppress_initial_callbacks = false, inject_resume_failure = false;
};

class AudioStartupProbe : public RefCounted {
    GDCLASS(AudioStartupProbe, RefCounted)
protected:
    static void _bind_methods() { ClassDB::bind_method(D_METHOD("run"), &AudioStartupProbe::run); }
public:
    Dictionary run() {
        OPUS_MULTISTREAM_CONFIGURATION config{};
        config.sampleRate = 48000; config.channelCount = 2; config.streams = 1;
        config.coupledStreams = 1; config.samplesPerFrame = 240;
        config.mapping[0] = 0; config.mapping[1] = 1;
        Ref<StartupTestRenderer> renderer; renderer.instantiate();
        assert(!renderer->startup_watch());
        assert(renderer->init(0, nullptr, nullptr, 0) < 0);
        auto invalid = renderer->startup_watch();
        assert(invalid->snapshot().state() == AudioStartupState::failed && invalid->snapshot().first_error() != 0);
        renderer->suppress_initial_callbacks = true;
        assert(renderer->init(0, &config, nullptr, 0) == 0);
        auto init_only = renderer->startup_watch();
        assert(init_only->snapshot().state() == AudioStartupState::not_started && !init_only->snapshot().ready());
        renderer->start();
        auto first = renderer->startup_watch();
        assert(first->identity().lifetime == renderer->lifetime());
        assert(first->identity().attempt == 1);
        assert(first->snapshot().backend().facts().identity.attempt == 2); // init already started backend once.
        const auto dispatches = renderer->backend->dispatches.load();
        until([&] { return renderer->backend->dispatches.load() >= dispatches + 3; });
        assert(!first->snapshot().ready());
        assert(first->snapshot().backend().facts().start_returned_success);
        renderer->backend->suppress = false;
        until([&] { return first->snapshot().ready(); });
        assert(!init_only->snapshot().ready() && !invalid->snapshot().ready());

        // First real Opus sample is created/delivered after observed readiness.
        unsigned char mapping[2] = {0, 1}; int error = OPUS_OK;
        auto *encoder = opus_multistream_encoder_create(48000, 2, 1, 1, mapping, OPUS_APPLICATION_AUDIO, &error);
        assert(encoder && error == OPUS_OK);
        float pcm[480] = {}; unsigned char packet[4000];
        const int bytes = opus_multistream_encode_float(encoder, pcm, 240, packet, sizeof(packet));
        opus_multistream_encoder_destroy(encoder);
        assert(bytes > 0);
        Ref<OpusDecoderWrapper> reference; reference.instantiate();
        PackedByteArray opus_mapping; opus_mapping.resize(2); opus_mapping[0] = 0; opus_mapping[1] = 1;
        PackedByteArray compressed; compressed.resize(bytes); std::memcpy(compressed.ptrw(), packet, bytes);
        assert(reference->init(48000, 2, 1, 1, opus_mapping) == 0);
        assert(reference->decode(compressed, 240) == 240 && reference->get_last_pcm().size() == 480);
        reference->cleanup();
        reference.unref();
        renderer->decode_and_play_sample(reinterpret_cast<char *>(packet), bytes);
        renderer->stop();
        assert(!first->snapshot().ready() && first->snapshot().state() == AudioStartupState::paused);
        renderer->start();
        auto second = renderer->startup_watch();
        assert(second->identity().attempt > first->identity().attempt);
        until([&] { return second->snapshot().ready(); });
        assert(!first->snapshot().ready());

        renderer->backend->hold = true;
        until([&] { return renderer->backend->entered.load(); });
        auto *held_backend = renderer->backend;
        std::atomic<bool> closed{false};
        std::thread close([&] {
            assert(renderer->cleanup_observed(second->identity().lifetime).clean_active_shutdown());
            closed = true;
        });
        until([&] { return second->snapshot().state() == AudioStartupState::closed; });
        assert(!closed && !second->snapshot().ready());
        held_backend->release = true;
        close.join();
        assert(closed && !second->snapshot().ready());

        renderer->suppress_initial_callbacks = false;
        renderer->inject_resume_failure = true;
        assert(renderer->init(0, &config, nullptr, 0) == 0);
        renderer->start(); // ABI is still void: the watch records actual failure.
        auto failed = renderer->startup_watch();
        assert(failed->snapshot().state() == AudioStartupState::failed && !failed->snapshot().ready());
        assert(failed->snapshot().backend().facts().first_error == MA_ERROR);
        renderer->backend->fail_resume = false;
        renderer->start();
        auto retry = renderer->startup_watch();
        assert(!retry->snapshot().ready() && retry->snapshot().backend().facts().first_error == MA_ERROR);
        assert(renderer->cleanup_observed(renderer->lifetime()).resources_released());
        renderer->inject_resume_failure = false;
        assert(renderer->init(0, &config, nullptr, 0) == 0);
        renderer->start();
        auto fresh = renderer->startup_watch();
        until([&] { return fresh->snapshot().ready(); });
        assert(fresh->identity().lifetime.generation > failed->identity().lifetime.generation);
        assert(!failed->snapshot().ready());
        renderer->cleanup();
        config.streams = 0;
        assert(renderer->init(0, &config, nullptr, 0) < 0);
        assert(renderer->startup_watch()->snapshot().state() == AudioStartupState::failed);
        renderer.unref();
        assert(!fresh->snapshot().ready() && !failed->snapshot().ready());
        Dictionary result;
        result["passed"] = true;
        result["backend"] = "production miniaudio Null";
        result["actual_opus_version"] = opus_get_version_string();
        result["independent_opus_decoded_samples_after_ready"] = 480;
        result["renderer_opus_packet_delivered_after_ready"] = true;
        result["renderer_backend_attempts_separate"] = true;
        result["start_void_failure_observed"] = true;
        result["actual_callback_shutdown_race"] = true;
        result["transport_ready_issued"] = false;
        result["physical_dac_or_quest_verified"] = false;
        return result;
    }
};

extern "C" GDExtensionBool GDE_EXPORT audio_startup_probe_init(GDExtensionInterfaceGetProcAddress address,
        GDExtensionClassLibraryPtr library, GDExtensionInitialization *initialization) {
    GDExtensionBinding::InitObject init(address, library, initialization);
    init.register_initializer([](ModuleInitializationLevel level) {
        if (level != MODULE_INITIALIZATION_LEVEL_SCENE) return;
        ClassDB::register_class<OpusDecoderWrapper>();
        ClassDB::register_class<AudioRenderer>();
        ClassDB::register_class<StartupTestRenderer>();
        ClassDB::register_class<AudioStartupProbe>();
    });
    init.set_minimum_library_initialization_level(MODULE_INITIALIZATION_LEVEL_SCENE);
    return init.init();
}
