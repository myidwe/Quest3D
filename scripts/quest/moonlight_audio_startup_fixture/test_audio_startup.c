// Actual pinned AudioStream.c + RTP queue + pthreads + loopback UDP + libopus.
// Policy injection exists only in this private translation unit; it is NOT an
// authenticated profile, host-send gate, device callback or Quest readiness proof.
#include <pthread.h>
#include <stdatomic.h>
#include <errno.h>
#include <limits.h>
#include <math.h>
#include <time.h>
#include "Limelight-internal.h"
#include <opus.h>

static pthread_mutex_t testMutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t testCond = PTHREAD_COND_INITIALIZER;
static int failThread;
static atomic_int failMalloc;
static atomic_int failRecv;
static atomic_int recvCalls;
static atomic_int recvPackets;
static atomic_int joinedWorkers;
static int holdThread;
static bool threadHeld, releaseThread;
static bool policyAllowed = true;

static bool Quest3dFixtureAuthorizeEndpoint(void* context, uint64_t serial, SOCKET socket, uint16_t port) {
    assert(context && serial && socket >= 0 && port);
    return policyAllowed;
}

static void notify_test(void) {
    pthread_mutex_lock(&testMutex);
    pthread_cond_broadcast(&testCond);
    pthread_mutex_unlock(&testMutex);
}
static int fixture_create(const char* name, ThreadEntry entry, void* context, PLT_THREAD* thread) {
    int type = !strcmp(name, "AudioRecv") ? 1 : !strcmp(name, "AudioDec") ? 2 : 3;
    if (failThread == type) return EAGAIN;
    pthread_mutex_lock(&testMutex);
    if (holdThread == type) {
        threadHeld = true;
        pthread_cond_broadcast(&testCond);
        while (!releaseThread) pthread_cond_wait(&testCond, &testMutex);
    }
    pthread_mutex_unlock(&testMutex);
    return PltCreateThread(name, entry, context, thread);
}
static void fixture_join(PLT_THREAD* thread) {
    PltJoinThread(thread);
    atomic_fetch_add(&joinedWorkers, 1);
}
static void* fixture_malloc(size_t size) {
    return atomic_exchange(&failMalloc, 0) ? NULL : malloc(size);
}
static int fixture_recv(SOCKET socket, char* buffer, int size, bool select) {
    atomic_fetch_add(&recvCalls, 1);
    notify_test();
    if (atomic_exchange(&failRecv, 0)) { errno = EBADF; return -1; }
    int result = recvUdpSocket(socket, buffer, size, select);
    if (result > 0) atomic_fetch_add(&recvPackets, 1);
    notify_test();
    return result;
}
#define PltCreateThread fixture_create
#define PltJoinThread fixture_join
#define malloc fixture_malloc
#define recvUdpSocket fixture_recv
#include "AudioStream.c"
#undef PltCreateThread
#undef PltJoinThread
#undef malloc
#undef recvUdpSocket

enum { FRAME_COUNT = 96, SAMPLES = 480, CHANNELS = 2, MAX_OPUS = 300 };
static unsigned char opusFrames[FRAME_COUNT][MAX_OPUS];
static int opusLengths[FRAME_COUNT];
static opus_int16 referencePcm[FRAME_COUNT][SAMPLES * CHANNELS];
static OpusDecoder* decoder;
static int decoded[FRAME_COUNT * 2], decodedCount, plcCount;
static bool compareReference;
static int initFailure, initCount, startCount, stopCount, cleanupCount;
static uint64_t currentSerial, previousSerial;
static uint32_t observedBits;
static int observedError, callbackCount;
static bool sawTerminal, everProgressComplete;
static int callbackHoldMask;
static bool callbackHeld, releaseCallback;
static void* expectedContext;
static atomic_int startResult, stopReturned;
static SOCKET peerSocket;
static struct sockaddr_in clientAddress;
static FILE* traceFile;

static struct timespec deadline(void) {
    struct timespec value;
    clock_gettime(CLOCK_REALTIME, &value);
    value.tv_sec += 5;
    return value;
}
#define WAIT_TEST(predicate) do { \
    struct timespec until = deadline(); \
    pthread_mutex_lock(&testMutex); \
    while (!(predicate)) assert(pthread_cond_timedwait(&testCond, &testMutex, &until) == 0); \
    pthread_mutex_unlock(&testMutex); \
} while (0)

static void state_callback(void* context, uint64_t serial, uint32_t bits, uint16_t port, int error) {
    pthread_mutex_lock(&testMutex);
    assert(context == expectedContext);
    if (!currentSerial) { assert(serial > previousSerial); currentSerial = serial; }
    assert(serial == currentSerial);
    if (sawTerminal) assert(!(bits & LI_AUDIO_STARTUP_PROGRESS_MASK));
    sawTerminal |= (bits & LI_AUDIO_STARTUP_TERMINAL_MASK) != 0;
    if (observedError) assert(error == observedError);
    if (bits & LI_AUDIO_STARTUP_LOCAL_SOCKET_BOUND) assert(port);
    assert(bits & LI_AUDIO_STARTUP_ENDPOINT_UNCONFIRMED);
    observedBits = bits;
    observedError = error;
    callbackCount++;
    if (traceFile) fprintf(traceFile, "{\"event\":\"core_state\",\"us\":%llu,\"serial\":%llu,\"bits\":%u,\"local_port\":%u,\"peer_port\":%u,\"first_error\":%d}\n",
        (unsigned long long)PltGetMicroseconds(), (unsigned long long)serial, bits, port, AudioPortNumber, error);
    if ((bits & LI_AUDIO_STARTUP_PROGRESS_MASK) == LI_AUDIO_STARTUP_PROGRESS_MASK)
        everProgressComplete = true;
    if (callbackHoldMask && (bits & callbackHoldMask) && !callbackHeld) {
        callbackHeld = true;
        pthread_cond_broadcast(&testCond);
        while (!releaseCallback) pthread_cond_wait(&testCond, &testMutex);
    }
    pthread_cond_broadcast(&testCond);
    pthread_mutex_unlock(&testMutex);
}
static void terminated(int error) { assert(error); }
static int renderer_init(int config, const POPUS_MULTISTREAM_CONFIGURATION opusConfig, void* context, int flags) {
    (void)config; (void)flags;
    assert(context == expectedContext && opusConfig->samplesPerFrame == SAMPLES);
    initCount++;
    if (initFailure) return initFailure;
    int error;
    decoder = opus_decoder_create(48000, CHANNELS, &error);
    assert(decoder && error == OPUS_OK);
    return 0;
}
static void renderer_start(void) { startCount++; }
static void renderer_stop(void) { stopCount++; }
static void renderer_cleanup(void) { cleanupCount++; opus_decoder_destroy(decoder); decoder = NULL; }
static void renderer_decode(char* bytes, int length) {
    opus_int16 pcm[SAMPLES * CHANNELS];
    int marker = -1;
    if (length) {
        for (int i = 0; i < FRAME_COUNT; ++i)
            if (length == opusLengths[i] && !memcmp(bytes, opusFrames[i], length)) { marker = i; break; }
        assert(marker >= 0);
    }
    int count = opus_decode(decoder, (unsigned char*)bytes, length, pcm, SAMPLES, 0);
    assert(count == SAMPLES);
    if (compareReference && marker >= 0) assert(!memcmp(pcm, referencePcm[marker], sizeof(pcm)));
    pthread_mutex_lock(&testMutex);
    if (marker < 0) plcCount++;
    assert(decodedCount < (int)(sizeof(decoded) / sizeof(decoded[0])));
    decoded[decodedCount++] = marker;
    if (traceFile) fprintf(traceFile, "{\"event\":\"actual_opus_decode\",\"us\":%llu,\"serial\":%llu,\"marker\":%d,\"samples\":%d,\"reference_pcm_compared\":%s}\n",
        (unsigned long long)PltGetMicroseconds(), (unsigned long long)currentSerial, marker, count,
        compareReference && marker >= 0 ? "true" : "false");
    pthread_cond_broadcast(&testCond);
    pthread_mutex_unlock(&testMutex);
}

static void make_opus(void) {
    int error;
    OpusEncoder* encoder = opus_encoder_create(48000, CHANNELS, OPUS_APPLICATION_RESTRICTED_LOWDELAY, &error);
    assert(encoder && !error);
    assert(opus_encoder_ctl(encoder, OPUS_SET_BITRATE(128000)) == OPUS_OK);
    assert(opus_encoder_ctl(encoder, OPUS_SET_VBR(0)) == OPUS_OK);
    OpusDecoder* reference = opus_decoder_create(48000, CHANNELS, &error);
    assert(reference && !error);
    for (int frame = 0; frame < FRAME_COUNT; ++frame) {
        opus_int16 pcm[SAMPLES * CHANNELS];
        for (int sample = 0; sample < SAMPLES; ++sample) {
            double t = (frame * SAMPLES + sample) / 48000.0;
            pcm[2 * sample] = (opus_int16)(11000 * sin(2 * 3.141592653589793 * (210 + 13 * frame) * t));
            pcm[2 * sample + 1] = (opus_int16)(9000 * cos(2 * 3.141592653589793 * (380 + 7 * frame) * t));
        }
        opusLengths[frame] = opus_encode(encoder, pcm, SAMPLES, opusFrames[frame], MAX_OPUS);
        assert(opusLengths[frame] == 160);
        assert(opus_decode(reference, opusFrames[frame], opusLengths[frame], referencePcm[frame], SAMPLES, 0) == SAMPLES);
    }
    opus_encoder_destroy(encoder);
    opus_decoder_destroy(reference);
}

static void reset_case(bool fresh, bool callback) {
    previousSerial = currentSerial;
    currentSerial = observedBits = observedError = callbackCount = 0;
    sawTerminal = everProgressComplete = false;
    decodedCount = plcCount = 0;
    initFailure = initCount = startCount = stopCount = cleanupCount = 0;
    callbackHoldMask = holdThread = failThread = 0;
    callbackHeld = releaseCallback = threadHeld = releaseThread = false;
    atomic_store(&failMalloc, 0); atomic_store(&failRecv, 0);
    atomic_store(&recvCalls, 0); atomic_store(&recvPackets, 0); atomic_store(&joinedWorkers, 0);
    atomic_store(&startResult, INT_MIN); atomic_store(&stopReturned, 0);
    compareReference = fresh;
    policyAllowed = true;
    expectedContext = &currentSerial;
    LiInitializeAudioCallbacks(&AudioCallbacks);
    AudioCallbacks.init = renderer_init;
    AudioCallbacks.start = renderer_start;
    AudioCallbacks.stop = renderer_stop;
    AudioCallbacks.cleanup = renderer_cleanup;
    AudioCallbacks.decodeAndPlaySample = renderer_decode;
    AudioCallbacks.startupState = callback ? state_callback : NULL;
    AudioCallbacks.capabilities = fresh ? CAPABILITY_QUEST3D_FILE_PCM_FRESH_START : 0;
    ListenerCallbacks.connectionTerminated = terminated;
    memset(AppVersionQuad, 0, sizeof(AppVersionQuad));
    AppVersionQuad[0] = 7; AppVersionQuad[1] = 1; AppVersionQuad[2] = 500; AppVersionQuad[3] = -1;
    AudioPacketDuration = 10;
    AudioEncryptionEnabled = false;
    HighQualitySurroundEnabled = false;
    NormalQualityOpusConfig.channelCount = CHANNELS;
    NormalQualityOpusConfig.streams = 1;
    NormalQualityOpusConfig.coupledStreams = 1;
    NormalQualityOpusConfig.sampleRate = 48000;
    StreamConfig.audioConfiguration = AUDIO_CONFIGURATION_STEREO;
    memset(&RemoteAddr, 0, sizeof(RemoteAddr));
    memset(&LocalAddr, 0, sizeof(LocalAddr));
    struct sockaddr_in* remote = (struct sockaddr_in*)&RemoteAddr;
    struct sockaddr_in* local = (struct sockaddr_in*)&LocalAddr;
    local->sin_family = remote->sin_family = AF_INET;
    local->sin_addr.s_addr = remote->sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    AddrLen = sizeof(*remote);
    peerSocket = socket(AF_INET, SOCK_DGRAM, 0);
    assert(peerSocket >= 0 && bind(peerSocket, (struct sockaddr*)remote, AddrLen) == 0);
    socklen_t size = AddrLen;
    assert(getsockname(peerSocket, (struct sockaddr*)remote, &size) == 0);
    AudioPortNumber = ntohs(remote->sin_port);
}
static int initialize_case(void) {
    int result = initializeAudioStream(expectedContext);
    if (result) return result;
    result = notifyAudioPortNegotiationComplete();
    if (result) return result;
    socklen_t length = sizeof(clientAddress);
    assert(getsockname(rtpSocket, (struct sockaddr*)&clientAddress, &length) == 0);
    return 0;
}
static void close_case(bool started, bool initialized) {
    if (started) stopAudioStream();
    if (initialized) destroyAudioStream();
    close(peerSocket);
    if (currentSerial) assert(sawTerminal);
    int before = callbackCount;
    QaReport(LI_AUDIO_STARTUP_START_COMMITTED, 0, 0);
    assert(callbackCount == before);
}
static void start_case(void) {
    assert(initialize_case() == 0);
    assert(startAudioStream(expectedContext, 0) == 0);
    if (AudioCallbacks.startupState)
        WAIT_TEST((observedBits & LI_AUDIO_STARTUP_PROGRESS_MASK) == LI_AUDIO_STARTUP_PROGRESS_MASK);
    WAIT_TEST(atomic_load(&recvCalls) > 0);
    assert(decodedCount == 0);
}
static void send_wire(SOCKET sender, const unsigned char* packet, int size) {
    assert(sendto(sender, packet, size, 0, (struct sockaddr*)&clientAddress, sizeof(clientAddress)) == size);
}
static void send_data(int marker, uint16_t sequence, SOCKET sender) {
    unsigned char bytes[sizeof(RTP_PACKET) + MAX_OPUS] = {0};
    PRTP_PACKET packet = (PRTP_PACKET)bytes;
    packet->header = 0x80; packet->packetType = 97;
    packet->sequenceNumber = BE16(sequence); packet->timestamp = BE32((uint32_t)sequence * AudioPacketDuration);
    packet->ssrc = BE32(1);
    memcpy(packet + 1, opusFrames[marker], opusLengths[marker]);
    if (traceFile) fprintf(traceFile, "{\"event\":\"fixture_udp_send\",\"us\":%llu,\"serial\":%llu,\"sequence\":%u,\"marker\":%d,\"expected_peer\":%s}\n",
        (unsigned long long)PltGetMicroseconds(), (unsigned long long)currentSerial, sequence, marker,
        sender == peerSocket ? "true" : "false");
    send_wire(sender, bytes, sizeof(RTP_PACKET) + opusLengths[marker]);
}
static void send_and_consume(int marker, uint16_t sequence) {
    int packets = atomic_load(&recvPackets);
    int calls = atomic_load(&recvCalls);
    send_data(marker, sequence, peerSocket);
    WAIT_TEST(atomic_load(&recvPackets) > packets && atomic_load(&recvCalls) > calls);
}
static void send_fec(int block, int shard) {
    unsigned char parity[2][MAX_OPUS] = {{0}};
    unsigned char* shards[RTPA_TOTAL_SHARDS];
    for (int i = 0; i < 4; ++i) shards[i] = opusFrames[block + i];
    shards[4] = parity[0]; shards[5] = parity[1];
    assert(reed_solomon_encode(rtpAudioQueue.rs, shards, 6, opusLengths[block]) == 0);
    unsigned char bytes[sizeof(RTP_PACKET) + sizeof(AUDIO_FEC_HEADER) + MAX_OPUS] = {0};
    PRTP_PACKET packet = (PRTP_PACKET)bytes;
    packet->header = 0x80; packet->packetType = 127;
    PAUDIO_FEC_HEADER fec = (PAUDIO_FEC_HEADER)(packet + 1);
    fec->fecShardIndex = shard; fec->payloadType = 97;
    fec->baseSequenceNumber = BE16(block); fec->baseTimestamp = BE32(block * AudioPacketDuration); fec->ssrc = BE32(1);
    memcpy(fec + 1, parity[shard], opusLengths[block]);
    int count = atomic_load(&recvPackets), calls = atomic_load(&recvCalls);
    send_wire(peerSocket, bytes, sizeof(RTP_PACKET) + sizeof(AUDIO_FEC_HEADER) + opusLengths[block]);
    WAIT_TEST(atomic_load(&recvPackets) > count && atomic_load(&recvCalls) > calls);
}
static void* start_on_owner(void* unused) { (void)unused; atomic_store(&startResult, startAudioStream(expectedContext, 0)); notify_test(); return NULL; }
static void* stop_on_owner(void* unused) { (void)unused; stopAudioStream(); atomic_store(&stopReturned, 1); notify_test(); return NULL; }

static void baseline_tests(void) {
    reset_case(false, true); start_case();
    for (int i = 0; i < 60; ++i) {
        send_and_consume(i, i);
        if (i >= 52) WAIT_TEST(decodedCount == i - 51);
    }
    assert(decodedCount == 8 && decoded[0] == 52 && !plcCount);
    close_case(true, true);
    puts("PASS actual legacy UDP: 50 packets/500 ms resync + first FEC boundary drop, first decode seq52");
#ifdef QUEST3D_AUDIO_STARTUP_FIXTURE
    reset_case(true, true); assert(initialize_case() == 0);
    // Counterfactual: production legacy queue while ONLY initial resync is bypassed.
    // This is not a product switch or an alternate implementation of the queue.
    RtpaCleanupQueue(&rtpAudioQueue); RtpaInitializeQueue(&rtpAudioQueue);
    compareReference = false;
    assert(startAudioStream(expectedContext, 0) == 0);
    WAIT_TEST((observedBits & LI_AUDIO_STARTUP_PROGRESS_MASK) == LI_AUDIO_STARTUP_PROGRESS_MASK);
    for (int i = 0; i < 8; ++i) { send_and_consume(i, i); if (i >= 4) WAIT_TEST(decodedCount == i - 3); }
    assert(decodedCount == 4 && decoded[0] == 4);
    close_case(true, true);
    puts("PASS actual UDP counterfactual: resync bypass alone still discards seq0..3 in legacy FEC queue");
#endif
}

static void fresh_tests(void) {
#ifdef QUEST3D_AUDIO_STARTUP_FIXTURE
    reset_case(true, true); start_case();
    for (int i = 0; i < 8; ++i) { send_and_consume(i, i); WAIT_TEST(decodedCount == i + 1); assert(decoded[i] == i); }
    send_and_consume(3, 3); assert(decodedCount == 8); // Duplicate is never replayed.
    SOCKET foreign = socket(AF_INET, SOCK_DGRAM, 0); assert(foreign >= 0);
    send_data(70, 8, foreign); close(foreign);
    send_and_consume(8, 8); WAIT_TEST(decodedCount == 9); assert(decoded[8] == 8);
    close_case(true, true);
    puts("PASS actual fresh UDP seq0..8 exact independent Opus PCM, duplicate rejection, connected-UDP wrong-peer rejection");
    reset_case(true, true); start_case();
    int order[] = { 3, 1, 0, 2 };
    for (int i = 0; i < 4; ++i) send_and_consume(order[i], order[i]);
    WAIT_TEST(decodedCount == 4);
    for (int i = 0; i < 4; ++i) assert(decoded[i] == i);
    close_case(true, true);
    puts("PASS actual fresh UDP reordered first FEC block -> ordered seq0..3");
    reset_case(true, true); assert(initialize_case() == 0);
    assert(RtpaSetKnownStart(&rtpAudioQueue, 65532) == 0);
    assert(startAudioStream(expectedContext, 0) == 0);
    WAIT_TEST((observedBits & LI_AUDIO_STARTUP_PROGRESS_MASK) == LI_AUDIO_STARTUP_PROGRESS_MASK);
    for (int i = 0; i < 8; ++i) { send_and_consume(i, (uint16_t)(65532 + i)); WAIT_TEST(decodedCount == i + 1); assert(decoded[i] == i); }
    close_case(true, true);
    puts("PASS actual UDP/FEC 16-bit wrap 65532..65535->0..3 does not reinitialize or skip block zero");
    reset_case(true, true); start_case();
    send_fec(0, 0); send_fec(0, 1);
    send_and_consume(1, 1); send_and_consume(2, 2);
#ifdef QUEST3D_TEST_RELEASE_FEC
    // The pinned debug queue intentionally withholds one extra shard to test
    // its synthetic drop validator. This separate production Release build has
    // the real 4-of-6 recovery threshold and can recover both absent shards.
    WAIT_TEST(decodedCount == 4);
    assert(rtpAudioQueue.stats.packetCountFecRecovered == 2);
#else
    send_and_consume(3, 3);
    WAIT_TEST(decodedCount == 4);
    assert(rtpAudioQueue.stats.packetCountFecRecovered == 1);
#endif
    for (int i = 0; i < 4; ++i) assert(decoded[i] == i);
    close_case(true, true);
    puts("PASS actual FEC-first UDP, production RS recovery and independent first-block Opus PCM");
    reset_case(true, true); start_case(); compareReference = false;
    send_and_consume(0, 0); WAIT_TEST(decodedCount == 1);
    send_and_consume(4, 4); WAIT_TEST(decodedCount == 5);
    assert(decoded[0] == 0 && decoded[1] == -1 && decoded[2] == -1 && decoded[3] == -1 && decoded[4] == 4);
    assert(plcCount == 3 && rtpAudioQueue.stats.packetCountFecFailed == 1);
    close_case(true, true);
    puts("PASS actual unrecoverable first-block loss produces 3 explicit Opus PLC frames, not fabricated markers");
#endif
}

static void rollback_tests(void) {
    for (int fail = 1; fail <= 3; ++fail) {
        reset_case(false, true); failThread = fail;
        int result = initialize_case();
        if (fail != 3) { assert(result == 0); result = startAudioStream(expectedContext, 0); }
        assert(result == EAGAIN && observedError == EAGAIN && !everProgressComplete);
        if (fail == 2) assert(atomic_load(&joinedWorkers) == 1);
        close_case(false, true);
        assert(atomic_load(&joinedWorkers) == (fail == 3 ? 0 : fail == 2 ? 2 : 1));
    }
    puts("PASS actual Ping/Recv/Decode thread-create rollback, joins and sticky error with no complete startup");
    reset_case(false, true); initFailure = -919;
    assert(initialize_case() == 0 && startAudioStream(expectedContext, 0) == -919);
    assert(observedError == -919 && startCount == 0 && !everProgressComplete);
    close_case(false, true);
    reset_case(false, true); assert(initialize_case() == 0); atomic_store(&failMalloc, 1);
    assert(startAudioStream(expectedContext, 0) == 0);
    WAIT_TEST(observedBits & LI_AUDIO_STARTUP_RECV_EXITED);
    assert(observedError == -1 && !everProgressComplete);
    close_case(true, true);
    reset_case(false, true); assert(initialize_case() == 0); atomic_store(&failRecv, 1);
    assert(startAudioStream(expectedContext, 0) == 0);
    WAIT_TEST(observedBits & LI_AUDIO_STARTUP_RECV_EXITED);
    assert(observedError == EBADF); // Pinned LastSocketFail() preserves positive errno.
    close_case(true, true);
    puts("PASS actual init/malloc/recv failure revokes state and owner shutdown joins callbacks/workers");
    reset_case(false, true); start_case();
    LbqSignalQueueShutdown(&packetQueue);
    WAIT_TEST(observedBits & LI_AUDIO_STARTUP_DECODE_EXITED);
    assert(!(observedBits & LI_AUDIO_STARTUP_PROGRESS_MASK));
    close_case(true, true);
    reset_case(false, true); start_case();
    interruptAudioStartup();
    assert(observedBits & LI_AUDIO_STARTUP_STOPPING);
    QaReport(LI_AUDIO_STARTUP_PROGRESS_MASK, 0, 0);
    assert(!(observedBits & LI_AUDIO_STARTUP_PROGRESS_MASK));
    close_case(true, true);
    puts("PASS actual decoder queue exit and interrupt revoke progress; late positive observations cannot revive terminal state");
}

static void barrier_tests(void) {
    reset_case(false, true); assert(initialize_case() == 0); holdThread = 2;
    pthread_t owner; assert(pthread_create(&owner, NULL, start_on_owner, NULL) == 0);
    WAIT_TEST(threadHeld && (observedBits & LI_AUDIO_STARTUP_RECV_ENTERED));
    assert(!(observedBits & LI_AUDIO_STARTUP_START_COMMITTED) && !everProgressComplete && !decodedCount);
    pthread_mutex_lock(&testMutex); releaseThread = true; pthread_cond_broadcast(&testCond); pthread_mutex_unlock(&testMutex);
    assert(pthread_join(owner, NULL) == 0 && atomic_load(&startResult) == 0);
    WAIT_TEST(everProgressComplete);
    close_case(true, true);
    puts("PASS real decoder-create barrier: receive entry alone cannot produce START_COMMITTED");
    reset_case(false, true); start_case();
    callbackHoldMask = LI_AUDIO_STARTUP_STOPPING;
    assert(pthread_create(&owner, NULL, stop_on_owner, NULL) == 0);
    WAIT_TEST(callbackHeld);
    assert(!atomic_load(&stopReturned));
    pthread_mutex_lock(&testMutex); releaseCallback = true; pthread_cond_broadcast(&testCond); pthread_mutex_unlock(&testMutex);
    assert(pthread_join(owner, NULL) == 0 && atomic_load(&stopReturned));
    assert(cleanupCount == 1 && atomic_load(&joinedWorkers) == 2);
    close_case(false, true);
    puts("PASS held real startup callback drains before stop returns; worker exit snapshots remain terminal");
    reset_case(false, true); start_case();
    callbackHoldMask = LI_AUDIO_STARTUP_RECV_EXITED;
    assert(pthread_create(&owner, NULL, stop_on_owner, NULL) == 0);
    WAIT_TEST(callbackHeld);
    assert(!atomic_load(&stopReturned) && cleanupCount == 0);
    pthread_mutex_lock(&testMutex); releaseCallback = true; pthread_cond_broadcast(&testCond); pthread_mutex_unlock(&testMutex);
    assert(pthread_join(owner, NULL) == 0 && atomic_load(&stopReturned));
    assert(cleanupCount == 1 && atomic_load(&joinedWorkers) == 2);
    close_case(false, true);
    puts("PASS held actual receive-worker exit callback prevents join/cleanup completion until it returns");
}

static void denial_tests(void) {
    reset_case(true, false);
    assert(initializeAudioStream(expectedContext) == LI_AUDIO_STARTUP_UNSUPPORTED_COMBINATION);
    close_case(false, false);
    reset_case(true, true); AudioCallbacks.capabilities |= CAPABILITY_DIRECT_SUBMIT;
    assert(initializeAudioStream(expectedContext) == LI_AUDIO_STARTUP_UNSUPPORTED_COMBINATION);
    close_case(false, false);
#ifndef QUEST3D_AUDIO_STARTUP_FIXTURE
    reset_case(true, true);
    assert(initializeAudioStream(expectedContext) == LI_AUDIO_STARTUP_ENDPOINT_POLICY_REQUIRED);
    assert(observedError == LI_AUDIO_STARTUP_ENDPOINT_POLICY_REQUIRED && !everProgressComplete && initCount == 0);
    close_case(false, false);
    puts("PASS production binary has no endpoint grant: capability+callback still rejected before resources/AR init");
#else
    reset_case(true, true); policyAllowed = false;
    assert(initialize_case() == LI_AUDIO_STARTUP_ENDPOINT_POLICY_REQUIRED);
    assert(observedError == LI_AUDIO_STARTUP_ENDPOINT_POLICY_REQUIRED && !everProgressComplete);
    close_case(false, true);
    puts("PASS fixture policy denial; missing callback and direct-submit combination rejected");
#endif
    reset_case(false, false); start_case(); close_case(true, true);
    assert(callbackCount == 0 && initCount == 1 && cleanupCount == 1);
    puts("PASS ordinary no-capability/null-observer lifecycle preserved");
}

int main(int argc, char** argv) {
    setvbuf(stdout, NULL, _IONBF, 0);
    if (argc == 2) { traceFile = fopen(argv[1], "w"); assert(traceFile); }
    assert(initializePlatform() == 0);
    make_opus();
    printf("libopus=%s; actual pinned AudioStream/RtpAudioQueue, actual pthread/UDP; fixture backend only\n", opus_get_version_string());
    baseline_tests(); fresh_tests(); rollback_tests(); barrier_tests(); denial_tests();
    cleanupPlatform();
    if (traceFile) assert(fclose(traceFile) == 0);
    puts("PASS all audio startup component cases; endpoint auth/history/global READY/device/Quest remain unproven");
    return 0;
}
