// Explicit Moonlight fixture: no socket or device. Production StreamConnection
// owns its real workers and invokes these blocking/unwind callbacks.
#include "moonlight_fixture.h"
extern "C" {
#include <Limelight.h>
}
#include <chrono>
#include <cstring>
#include <thread>
#include <cassert>

namespace fixture {
std::atomic<int> mode{normal};
std::atomic<int> duplicate_init_result{0};
std::atomic<bool> check_fixture_host{false};
std::atomic<bool> entered{false}, ready{false}, interrupted{false}, stop_entered{false}, hold_stop{false}, drain_failure{false};
AUDIO_RENDERER_CALLBACKS audio{};
CONNECTION_LISTENER_CALLBACKS listener{};
void terminate_current(int error) { listener.connectionTerminated(error); }
void reset(Mode selected) {
    mode=selected; entered=false; ready=false; interrupted=false; stop_entered=false; hold_stop=false; drain_failure=false; duplicate_init_result=0; audio={};
}
}
extern "C" {
void LiInitializeServerInformation(PSERVER_INFORMATION info) { std::memset(info,0,sizeof(*info)); }
void LiInitializeStreamConfiguration(PSTREAM_CONFIGURATION config) { std::memset(config,0,sizeof(*config)); }
void LiInitializeVideoCallbacks(PDECODER_RENDERER_CALLBACKS cb) { std::memset(cb,0,sizeof(*cb)); }
void LiInitializeAudioCallbacks(PAUDIO_RENDERER_CALLBACKS cb) { std::memset(cb,0,sizeof(*cb)); }
void LiInitializeConnectionCallbacks(PCONNECTION_LISTENER_CALLBACKS cb) { std::memset(cb,0,sizeof(*cb)); }
int LiStartConnection(PSERVER_INFORMATION server, PSTREAM_CONFIGURATION, PCONNECTION_LISTENER_CALLBACKS listener,
    PDECODER_RENDERER_CALLBACKS, PAUDIO_RENDERER_CALLBACKS audio, void*, int, void *audio_context, int flags) {
    fixture::entered=true;
    assert(!fixture::check_fixture_host || (server->address && std::strcmp(server->address,"fixture")==0));
    fixture::interrupted=false;
    fixture::listener=*listener;
    if (fixture::mode==fixture::cancelled_start) {
        while (!fixture::interrupted) std::this_thread::sleep_for(std::chrono::milliseconds(1));
        return -91;
    }
    if (fixture::mode==fixture::no_audio_failure) return -92;
    if (audio) {
        fixture::audio=*audio;
        OPUS_MULTISTREAM_CONFIGURATION opus{};
        opus.sampleRate=fixture::mode==fixture::opus_failure ? 123 : 48000;
        opus.channelCount=2; opus.streams=1; opus.coupledStreams=1; opus.samplesPerFrame=240;
        opus.mapping[0]=0; opus.mapping[1]=1;
        const int initialized=audio->init(AUDIO_CONFIGURATION_STEREO,&opus,audio_context,flags);
        if (initialized) return initialized;
        if (fixture::mode==fixture::duplicate_audio)
            fixture::duplicate_init_result=audio->init(AUDIO_CONFIGURATION_STEREO,&opus,audio_context,flags);
        audio->start();
    }
    listener->connectionStarted();
    fixture::ready=true;
    return 0;
}
void LiInterruptConnection() { fixture::interrupted=true; }
void LiStopConnection() {
    fixture::stop_entered=true;
    while (fixture::hold_stop) std::this_thread::sleep_for(std::chrono::milliseconds(1));
    if (fixture::audio.stop) fixture::audio.stop();
    if (fixture::audio.cleanup) fixture::audio.cleanup();
}
int LiWaitForConnectionCallbacks() { return fixture::drain_failure ? -1 : 0; }
const char *LiGetStageName(int) { return "fixture"; }
bool LiGetEstimatedRttInfo(uint32_t *rtt, uint32_t *variance) { *rtt=0; *variance=0; return true; }
bool LiGetHdrMetadata(PSS_HDR_METADATA) { return false; }
void LiRequestIdrFrame() {}
}
