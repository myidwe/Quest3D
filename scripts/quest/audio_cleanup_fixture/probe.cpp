#include "audio/audio_renderer.h"
#include "audio/opus_decoder.h"
#include <godot_cpp/godot.hpp>
#include <godot_cpp/variant/dictionary.hpp>
#include <godot_cpp/variant/utility_functions.hpp>
#include <cassert>
#include <cstring>
#include <thread>

using namespace godot;

class AudioCleanupProbe : public RefCounted {
    GDCLASS(AudioCleanupProbe, RefCounted)
protected:
    static void _bind_methods() { ClassDB::bind_method(D_METHOD("run"), &AudioCleanupProbe::run); }
public:
    Dictionary run() {
        unsigned char mapping[2] = {0, 1};
        int err = OPUS_OK;
        OpusMSEncoder *encoder = opus_multistream_encoder_create(48000,2,1,1,mapping,OPUS_APPLICATION_AUDIO,&err);
        assert(encoder && err == OPUS_OK);
        float silence[480] = {};
        unsigned char packet[4000];
        const int bytes = opus_multistream_encode_float(encoder,silence,240,packet,sizeof(packet));
        assert(bytes > 0);
        opus_multistream_encoder_destroy(encoder);
        Ref<OpusDecoderWrapper> opus;
        opus.instantiate();
        PackedByteArray map; map.resize(2); map[0]=0; map[1]=1;
        assert(opus->init(48000,2,1,1,map)==0);
        PackedByteArray data; data.resize(bytes); std::memcpy(data.ptrw(),packet,bytes);
        assert(opus->decode(data,240)==240);
        assert(opus->get_last_pcm().size()==480);
        auto decoder_closed = opus->cleanup_observed();
        assert(decoder_closed.had_decoder && decoder_closed.destroy_returned && decoder_closed.decoder_absent && decoder_closed.pcm_cleared);
        assert(opus->get_last_pcm().is_empty());
        assert(opus->decode(data,240)<0);
        auto decoder_duplicate = opus->cleanup_observed();
        assert(!decoder_duplicate.had_decoder && !decoder_duplicate.destroy_returned && decoder_duplicate.decoder_absent);
        Ref<AudioRenderer> renderer; renderer.instantiate();
        assert(!renderer->cleanup_observed(renderer->lifetime()).resources_released());
        assert(renderer->init(0,nullptr,nullptr,0)<0);
        auto invalid = renderer->cleanup_observed(renderer->lifetime());
        assert(invalid.resources_released() && !invalid.clean_active_shutdown());
        assert(invalid.facts().init_stage==nightfall::AudioInitStage::invalid_parameters);
        OPUS_MULTISTREAM_CONFIGURATION config{};
        config.sampleRate=48000; config.channelCount=2; config.streams=1;
        config.coupledStreams=1; config.samplesPerFrame=240;
        config.mapping[0]=0; config.mapping[1]=1;
        assert(renderer->init(0,&config,nullptr,0)==0);
        auto first=renderer->lifetime();
        assert(renderer->get_backend_name()==String("Null"));
        renderer->start();
        renderer->decode_and_play_sample(reinterpret_cast<char *>(packet),bytes);
        renderer->stop();
        assert(!renderer->last_cleanup_completion().resources_released());
        auto closed=renderer->cleanup_observed(first);
        assert(closed.clean_active_shutdown());
        assert(renderer->cleanup_observed(first).clean_active_shutdown());
        assert(!renderer->is_initialized() && renderer->get_backend_name()==String("none"));
        assert(renderer->init(0,&config,nullptr,0)==0);
        auto second=renderer->lifetime();
        assert(first.instance==second.instance && second.generation==first.generation+1);
        assert(renderer->cleanup_observed(first).facts().state==nightfall::AudioCleanupState::scope_mismatch);
        assert(renderer->is_initialized());
        renderer->start();
        // Actual decode/write races the cleanup mutex; after return no worker
        // can access retired Opus/ring storage. It may only observe inactive.
        std::thread decode([&] {
            for (int i=0;i<200;++i) renderer->decode_and_play_sample(reinterpret_cast<char *>(packet),bytes);
        });
        auto concurrent=renderer->cleanup_observed(second);
        decode.join();
        assert(concurrent.clean_active_shutdown());
        config.streams=0;
        assert(renderer->init(0,&config,nullptr,0)<0);
        auto opus_fail=renderer->cleanup_observed(renderer->lifetime());
        assert(opus_fail.resources_released() && !opus_fail.clean_active_shutdown());
        assert(opus_fail.facts().init_stage==nightfall::AudioInitStage::opus_failed && !opus_fail.facts().backend_created);
        Dictionary result;
        result["passed"]=true;
        result["actual_opus_version"]=opus_get_version_string();
        result["actual_decoded_samples"]=480;
        result["backend"]="miniaudio null only";
        result["actual_quest_speaker_verified"]=false;
        result["clean_active_shutdowns"]=2;
        result["partial_init_boundaries"]=2;
        result["old_lifetime_rejected"]=true;
        result["decoded_pcm_deleted"]=true;
        renderer.unref();
        assert(closed.clean_active_shutdown()); // Immutable old completion survives object destruction.
        return result;
    }
};

extern "C" GDExtensionBool GDE_EXPORT audio_cleanup_probe_init(GDExtensionInterfaceGetProcAddress address,GDExtensionClassLibraryPtr library,GDExtensionInitialization *initialization) {
    GDExtensionBinding::InitObject init(address,library,initialization);
    init.register_initializer([](ModuleInitializationLevel level) {
        if (level!=MODULE_INITIALIZATION_LEVEL_SCENE) return;
        ClassDB::register_class<OpusDecoderWrapper>();
        ClassDB::register_class<AudioRenderer>();
        ClassDB::register_class<AudioCleanupProbe>();
    });
    init.set_minimum_library_initialization_level(MODULE_INITIALIZATION_LEVEL_SCENE);
    return init.init();
}
