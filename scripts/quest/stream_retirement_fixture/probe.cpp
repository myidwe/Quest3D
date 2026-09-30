#include "moonlight_fixture.h"
#include "video/stream_connection.h"
#include "video/ffmpeg_decoder.h"
#include "video/texture_uploader.h"
#include "video/depth_bridge.h"
#include "audio/audio_renderer.h"
#include "audio/opus_decoder.h"
#include "input/input_bridge.h"
#include "nightfall_stream.h"
#include <godot_cpp/godot.hpp>
#include <godot_cpp/variant/utility_functions.hpp>
#include <cassert>
#include <chrono>
#include <thread>
#include <type_traits>
using namespace godot;
using namespace std::chrono_literals;
static_assert(!std::is_default_constructible_v<StreamRetirement>);
static_assert(!std::is_constructible_v<StreamRetirementWatch,StreamLifetime>);

class StreamRetirementProbe : public RefCounted {
    GDCLASS(StreamRetirementProbe,RefCounted)
    StreamConnection *connection_=nullptr;
    int started_events_=0;
    int terminated_events_=0;
    static void wait(const std::atomic<bool> &flag) {
        const auto until=std::chrono::steady_clock::now()+2s;
        while (!flag && std::chrono::steady_clock::now()<until) std::this_thread::sleep_for(1ms);
        assert(flag);
    }
    void start(fixture::Mode mode, const char *nonce="1234567890abcdef1234567890abcdef") {
        fixture::reset(mode);
        Dictionary config;
        config["quest3d_transport_epoch"]=nonce;
        connection_->start("fixture-no-network",Dictionary(),config);
        wait(fixture::entered);
        if (mode==fixture::normal || mode==fixture::duplicate_audio) wait(fixture::ready);
    }
    void on_started() { ++started_events_; }
    void on_terminated(int, const String &) { ++terminated_events_; }
protected:
    static void _bind_methods() {
        ClassDB::bind_method(D_METHOD("run"),&StreamRetirementProbe::run);
        ClassDB::bind_method(D_METHOD("finish"),&StreamRetirementProbe::finish);
    }
public:
    StreamRetirementProbe() {
        connection_=memnew(StreamConnection);
        connection_->connect("stream_started",callable_mp(this,&StreamRetirementProbe::on_started));
        connection_->connect("stream_terminated",callable_mp(this,&StreamRetirementProbe::on_terminated));
    }
    ~StreamRetirementProbe() { if (connection_) memdelete(connection_); }
    Dictionary run() {
        assert(!connection_->retirement_watch());
        assert(!connection_->stop_observed(connection_->stream_lifetime()));
        start(fixture::normal);
        auto first_lifetime=connection_->stream_lifetime();
        auto first_watch=connection_->retirement_watch();
        auto wrong=first_lifetime; ++wrong.generation;
        assert(!connection_->stop_observed(wrong));
        wrong=first_lifetime; ++wrong.instance;
        assert(!connection_->stop_observed(wrong));
        wrong=first_lifetime; wrong.transport_nonce[0]='f';
        assert(!connection_->stop_observed(wrong));
        assert(connection_->is_streaming() && !fixture::interrupted);
        fixture::hold_stop=true;
        std::thread stopping([&] { connection_->stop_observed(first_lifetime); });
        wait(fixture::stop_entered);
        assert(!first_watch->snapshot());
        fixture::hold_stop=false;
        stopping.join();
        auto first=first_watch->snapshot();
        assert(first && first->clean_active_cpu_shutdown() && !first->quest_native_closed());
        assert(first->audio_attempted() && first->audio_completion().clean_active_shutdown());
        assert(connection_->stop_observed(first_lifetime)==first);

        start(fixture::duplicate_audio);
        assert(fixture::duplicate_init_result==-1);
        auto duplicate=connection_->stop_observed(connection_->stream_lifetime());
        assert(duplicate && duplicate->clean_active_cpu_shutdown());
        assert(duplicate->audio_lifetime().generation==first->audio_lifetime().generation+1);

        start(fixture::no_audio_failure);
        auto failed=connection_->stop_observed(connection_->stream_lifetime());
        assert(failed && failed->resources_released() && !failed->clean_active_cpu_shutdown());
        assert(!failed->audio_attempted()); // Previous real AudioRenderer generation must not be reused.
        assert(first->audio_lifetime().valid() && first_watch->snapshot()==first);

        start(fixture::opus_failure);
        auto opus=connection_->stop_observed(connection_->stream_lifetime());
        assert(opus && opus->audio_attempted() && !opus->clean_active_cpu_shutdown());
        assert(opus->audio_lifetime().generation>first->audio_lifetime().generation);

        start(fixture::cancelled_start);
        const auto begin=std::chrono::steady_clock::now();
        auto cancelled=connection_->stop_observed(connection_->stream_lifetime());
        assert(std::chrono::steady_clock::now()-begin<1s); // The old decode setup wait was 10 seconds.
        assert(cancelled && !cancelled->clean_active_cpu_shutdown() && !cancelled->audio_attempted());

        start(fixture::normal,"not-a-valid-transport");
        auto invalid_nonce=connection_->stop_observed(connection_->stream_lifetime());
        assert(invalid_nonce && invalid_nonce->moonlight_stop_returned() && !invalid_nonce->resources_released());

        // Queue old stream_started, stop without pumping Godot, then queue a new
        // generation. Only the new generation may be emitted during idle delivery.
        start(fixture::normal);
        connection_->stop();
        start(fixture::normal);
        assert(!connection_->stop_observed(first_lifetime));
        assert(connection_->is_streaming() && !fixture::interrupted);
        Dictionary result;
        result["passed"]=true;
        result["actual_stream_stop_path"]=true;
        result["actual_audio_renderer_null_backend"]=true;
        result["actual_moonlight_network_session"]=false;
        result["actual_android_codec"]=false;
        return result;
    }
    Dictionary finish() {
        assert(started_events_==1 && terminated_events_==0);
        auto final=connection_->stop_observed(connection_->stream_lifetime());
        assert(final && final->clean_active_cpu_shutdown());
        auto retained=connection_->retirement_watch();
        memdelete(connection_); connection_=nullptr;
        assert(retained->snapshot()==final && !final->quest_native_closed());
        connection_=memnew(StreamConnection);
        start(fixture::normal);
        fixture::drain_failure=true;
        auto unconfirmed=connection_->stop_observed(connection_->stream_lifetime());
        assert(unconfirmed && !unconfirmed->termination_thread_drained() && !unconfirmed->resources_released());
        const auto quarantined_lifetime=connection_->stream_lifetime();
        fixture::entered=false;
        connection_->start("fixture",Dictionary(),Dictionary());
        assert(connection_->stream_lifetime()==quarantined_lifetime && !fixture::entered);
        auto other=memnew(StreamConnection);
        other->start("fixture",Dictionary(),Dictionary());
        assert(!other->retirement_watch() && !fixture::entered);
        memdelete(other);
        memdelete(connection_); connection_=nullptr;
        Dictionary result;
        result["passed"]=true;
        result["delivered_current_generation_events"]=started_events_;
        result["old_watch_survived_owner_destruction"]=true;
        result["stale_termination_events_dropped"]=terminated_events_==0;
        result["failed_drain_blocks_all_new_owners"]=true;
        return result;
    }
};

class StreamStartRefusalProbe : public RefCounted {
    GDCLASS(StreamStartRefusalProbe, RefCounted)
    StreamConnection *other_ = nullptr;
    static void wait_ready() {
        const auto until = std::chrono::steady_clock::now() + 2s;
        while (!fixture::ready && std::chrono::steady_clock::now() < until) std::this_thread::sleep_for(1ms);
        assert(fixture::ready);
    }
    static Dictionary config() {
        Dictionary value;
        value["quest3d_transport_epoch"]="abcdef0123456789abcdef0123456789";
        return value;
    }
protected:
    static void _bind_methods() {
        ClassDB::bind_method(D_METHOD("occupy"), &StreamStartRefusalProbe::occupy);
        ClassDB::bind_method(D_METHOD("release"), &StreamStartRefusalProbe::release);
        ClassDB::bind_method(D_METHOD("assert_other_running"), &StreamStartRefusalProbe::assert_other_running);
        ClassDB::bind_method(D_METHOD("start_normal", "stream"), &StreamStartRefusalProbe::start_normal);
        ClassDB::bind_method(D_METHOD("request_failure"), &StreamStartRefusalProbe::request_failure);
        ClassDB::bind_method(D_METHOD("fail_drain"), &StreamStartRefusalProbe::fail_drain);
        ClassDB::bind_method(D_METHOD("fail_future_starts"), &StreamStartRefusalProbe::fail_future_starts);
    }
public:
    StreamStartRefusalProbe() { fixture::check_fixture_host=true; }
    ~StreamStartRefusalProbe() { release(); }
    void occupy() {
        assert(!other_);
        fixture::reset(fixture::normal);
        other_=memnew(StreamConnection);
        assert(other_->start("fixture", Dictionary(), config()) == StreamConnection::START_ACCEPTED);
        wait_ready();
    }
    void release() { if (other_) { memdelete(other_); other_=nullptr; } }
    void assert_other_running() { assert(other_ && other_->is_streaming() && !fixture::interrupted); }
    int start_normal(NightfallStream *stream) {
        fixture::reset(fixture::normal);
        const int result=stream->start_stream("fixture", Dictionary(), config());
        if (result==StreamConnection::START_ACCEPTED) wait_ready();
        return result;
    }
    void request_failure() { fixture::terminate_current(-91); }
    void fail_drain() { fixture::drain_failure=true; }
    void fail_future_starts() { fixture::mode=fixture::no_audio_failure; }
};

extern "C" GDExtensionBool GDE_EXPORT stream_retirement_probe_init(GDExtensionInterfaceGetProcAddress address,GDExtensionClassLibraryPtr library,GDExtensionInitialization *initialization) {
    GDExtensionBinding::InitObject init(address,library,initialization);
    init.register_initializer([](ModuleInitializationLevel level) {
        if (level!=MODULE_INITIALIZATION_LEVEL_SCENE) return;
        ClassDB::register_class<OpusDecoderWrapper>();
        ClassDB::register_class<AudioRenderer>();
        ClassDB::register_class<FfmpegDecoder>();
        ClassDB::register_class<TextureUploader>();
        ClassDB::register_class<InputBridge>();
        ClassDB::register_class<DepthBridge>();
        ClassDB::register_class<StreamConnection>();
        ClassDB::register_class<NightfallConfigManager>();
        ClassDB::register_class<NightfallComputerManager>();
        ClassDB::register_class<HttpRequester>();
        ClassDB::register_class<NightfallStream>();
        ClassDB::register_class<StreamRetirementProbe>();
        ClassDB::register_class<StreamStartRefusalProbe>();
    });
    init.set_minimum_library_initialization_level(MODULE_INITIALIZATION_LEVEL_SCENE);
    return init.init();
}
