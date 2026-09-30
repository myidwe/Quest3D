"""Reconstruct a private pinned tree and generate/verify the fourth local patch."""
from pathlib import Path
import difflib
import shutil
import subprocess
import sys

root = Path(__file__).resolve().parents[2]
overlay = root / 'third_party/nightfall/addons/nightfall-stream/vcpkg-overlay/moonlight-common-c'
out = root / 'artifacts/quest/moonlight-audio-startup/source'
base = root / 'artifacts/quest/moonlight-audio-startup/base'
subprocess.run([sys.executable, str(root / 'scripts/quest/prepare_moonlight_callback_drain.py'),
                '--output', str(base)], check=True)
patch = overlay / '0004-quest3d-audio-startup.patch'
previous_patch = patch.read_text(encoding='utf-8') if patch.exists() else None

def replace(text, old, new):
    assert text.count(old) == 1, (old[:80], text.count(old))
    return text.replace(old, new, 1)

if '--generate' in sys.argv:
    changes = {}
    header = (base / 'src/Limelight.h').read_text(encoding='utf-8')
    extra = '''// Quest3D pinned extension: appended callback member changes the binary ABI.
// Rebuild the core and every client together. No global/backend/physical READY.
#define LI_HAS_AUDIO_STARTUP_OBSERVATIONS 1
#define CAPABILITY_QUEST3D_FILE_PCM_FRESH_START 0x80
#define LI_AUDIO_STARTUP_LOCAL_SOCKET_BOUND 0x0001u
#define LI_AUDIO_STARTUP_RECV_ENTERED 0x0002u
#define LI_AUDIO_STARTUP_DECODE_ENTERED 0x0004u
#define LI_AUDIO_STARTUP_START_COMMITTED 0x0008u
#define LI_AUDIO_STARTUP_ENDPOINT_UNCONFIRMED 0x0010u
#define LI_AUDIO_STARTUP_STOPPING 0x0100u
#define LI_AUDIO_STARTUP_FAILED 0x0200u
#define LI_AUDIO_STARTUP_RECV_EXITED 0x0400u
#define LI_AUDIO_STARTUP_DECODE_EXITED 0x0800u
#define LI_AUDIO_STARTUP_PROGRESS_MASK 0x000fu
#define LI_AUDIO_STARTUP_TERMINAL_MASK 0x0f00u
#define LI_AUDIO_STARTUP_UNSUPPORTED_COMBINATION (-5801)
#define LI_AUDIO_STARTUP_ENDPOINT_POLICY_REQUIRED (-5802)
#define LI_AUDIO_STARTUP_INVALID_LIFETIME (-5803)
#define LI_AUDIO_STARTUP_INVALID_ENDPOINT (-5804)
// Synchronous owner/worker observation with immutable context/serial per attempt.
// Serialized current snapshots: terminal bits clear progress and never recover.
// Never call lifecycle APIs from this callback. It must return promptly; stop/
// destroy drain it before returning. LOCAL_SOCKET_BOUND is not peer/epoch proof.
typedef void (*AudioRendererStartupState)(void* audioContext, uint64_t startupSerial,
                                        uint32_t stateBits, uint16_t localPort, int firstError);

'''
    changes['Limelight.h'] = replace(header, 'typedef struct _AUDIO_RENDERER_CALLBACKS {', extra + 'typedef struct _AUDIO_RENDERER_CALLBACKS {')
    changes['Limelight.h'] = replace(changes['Limelight.h'], '    int capabilities;\n} AUDIO_RENDERER_CALLBACKS', '    int capabilities;\n    AudioRendererStartupState startupState; // Optional; initialize callbacks to zero.\n} AUDIO_RENDERER_CALLBACKS')
    internal = (base / 'src/Limelight-internal.h').read_text(encoding='utf-8')
    changes['Limelight-internal.h'] = replace(internal, 'int initializeAudioStream(void);', 'int initializeAudioStream(void* audioContext);\nvoid interruptAudioStartup(void);')
    connection = (base / 'src/Connection.c').read_text(encoding='utf-8')
    changes['Connection.c'] = replace(connection, 'err = initializeAudioStream();', 'err = initializeAudioStream(audioContext);')
    changes['Connection.c'] = replace(changes['Connection.c'], '    ConnectionInterrupted = true;\n    ClDrainDisable();', '    ConnectionInterrupted = true;\n    ClDrainDisable();\n    interruptAudioStartup();')
    queue_h = (base / 'src/RtpAudioQueue.h').read_text(encoding='utf-8')
    changes['RtpAudioQueue.h'] = replace(queue_h, '    bool synchronizing;', '    bool synchronizing;\n    bool knownStart; // Separate from sequence zero, which is a valid start/wrap value.')
    changes['RtpAudioQueue.h'] = replace(changes['RtpAudioQueue.h'], 'void RtpaCleanupQueue', '// Call before any packets; seed must be the agreed FEC block boundary.\nint RtpaSetKnownStart(PRTP_AUDIO_QUEUE queue, uint16_t sequence);\nvoid RtpaCleanupQueue')
    queue = (base / 'src/RtpAudioQueue.c').read_text(encoding='utf-8')
    changes['RtpAudioQueue.c'] = replace(queue, 'static void validateFecBlockState', '''int RtpaSetKnownStart(PRTP_AUDIO_QUEUE queue, uint16_t sequence) {
    if (sequence % RTPA_DATA_SHARDS || queue->blockHead || queue->freeBlockHead ||
        queue->stats.packetCountAudio || queue->stats.packetCountFec || queue->incompatibleServer) return -1;
    queue->knownStart = true;
    queue->synchronizing = false;
    queue->nextRtpSequenceNumber = queue->oldestRtpBaseSequenceNumber = sequence;
    return 0;
}

static void validateFecBlockState''')
    changes['RtpAudioQueue.c'] = replace(changes['RtpAudioQueue.c'], 'if (queue->synchronizing && queue->oldestRtpBaseSequenceNumber == 0)', 'if (!queue->knownStart && queue->synchronizing && queue->oldestRtpBaseSequenceNumber == 0)')
    audio = (base / 'src/AudioStream.c').read_text(encoding='utf-8')
    audio = replace(audio, '#include "Limelight-internal.h"', '#include "Limelight-internal.h"\n#include "AudioStartup.h"')
    audio = replace(audio, 'static SOCKET rtpSocket', 'void interruptAudioStartup(void) { QaReport(LI_AUDIO_STARTUP_STOPPING, 0, 0); }\n\nstatic SOCKET rtpSocket')
    audio = replace(audio, 'int initializeAudioStream(void) {', '''int initializeAudioStream(void* audioContext) {
    int error = QaBegin(audioContext);
    if (error) return error;''')
    audio = replace(audio, '    LbqInitializeLinkedBlockingQueue(&packetQueue, 30);', '''    error = LbqInitializeLinkedBlockingQueue(&packetQueue, 30);
    if (error) {
        QaReport(0, error, 0);
        QaEnd();
        return error;
    }''')
    audio = replace(audio, '    RtpaInitializeQueue(&rtpAudioQueue);', '''    RtpaInitializeQueue(&rtpAudioQueue);
    if (qaStartup.fresh && RtpaSetKnownStart(&rtpAudioQueue, 0) != 0) {
        RtpaCleanupQueue(&rtpAudioQueue);
        LbqDestroyLinkedBlockingQueue(&packetQueue);
        QaReport(0, LI_AUDIO_STARTUP_UNSUPPORTED_COMBINATION, 0);
        QaEnd();
        return LI_AUDIO_STARTUP_UNSUPPORTED_COMBINATION;
    }''')
    audio = replace(audio, '    audioDecryptionCtx = PltCreateCryptoContext();', '''    audioDecryptionCtx = PltCreateCryptoContext();
    if (!audioDecryptionCtx) {
        RtpaCleanupQueue(&rtpAudioQueue);
        LbqDestroyLinkedBlockingQueue(&packetQueue);
        QaReport(0, -1, 0);
        QaEnd();
        return -1;
    }''')
    audio = replace(audio, '    if (rtpSocket == INVALID_SOCKET) {\n        return LastSocketFail();\n    }', '''    if (rtpSocket == INVALID_SOCKET) {
        int err = LastSocketFail();
        QaReport(0, err, 0);
        return err;
    }
    int socketError = QaObserveSocket(rtpSocket);
    if (socketError) {
        QaReport(0, socketError, 0);
        return socketError;
    }''')
    audio = replace(audio, '    if (err != 0) {\n        return err;\n    }\n\n    pingThreadStarted', '    if (err != 0) {\n        QaReport(0, err, 0);\n        return err;\n    }\n\n    pingThreadStarted')
    audio = replace(audio, 'void destroyAudioStream(void) {', 'void destroyAudioStream(void) {\n    QaReport(LI_AUDIO_STARTUP_STOPPING, 0, 0);')
    audio = replace(audio, '    RtpaCleanupQueue(&rtpAudioQueue);\n}', '    RtpaCleanupQueue(&rtpAudioQueue);\n    QaEnd();\n}')
    audio = replace(audio, '    packetsToDrop = 500 / AudioPacketDuration;', '    packetsToDrop = qaStartup.fresh ? 0 : 500 / AudioPacketDuration;\n    bool entered = false;')
    audio = replace(audio, '                Limelog("Audio Receive: malloc() failed\\n");', '                QaReport(0, -1, 0);\n                Limelog("Audio Receive: malloc() failed\\n");')
    audio = replace(audio, '        packet->header.size = recvUdpSocket', '''        if (!entered) {
            if (!QaReport(LI_AUDIO_STARTUP_RECV_ENTERED, 0, 0)) break;
            entered = true;
        }
        packet->header.size = recvUdpSocket''')
    audio = replace(audio, '''            Limelog("Audio Receive: recvUdpSocket() failed: %d\\n", (int)LastSocketError());
            ListenerCallbacks.connectionTerminated(LastSocketFail());''', '''            int socketError = LastSocketFail();
            QaReport(0, socketError, 0);
            Limelog("Audio Receive: recvUdpSocket() failed: %d\\n", socketError);
            ListenerCallbacks.connectionTerminated(socketError);''')
    audio = replace(audio, '            if (firstReceiveTime != 0)', '            if (!qaStartup.fresh && firstReceiveTime != 0)')
    audio = replace(audio, '    if (packet != NULL) {\n        free(packet);\n    }\n}', '    if (packet != NULL) {\n        free(packet);\n    }\n    QaReport(LI_AUDIO_STARTUP_RECV_EXITED, 0, 0);\n}')
    audio = replace(audio, '    while (!PltIsThreadInterrupted(&decoderThread)) {', '''    if (!QaReport(LI_AUDIO_STARTUP_DECODE_ENTERED, 0, 0)) {
        QaReport(LI_AUDIO_STARTUP_DECODE_EXITED, 0, 0);
        return;
    }
    while (!PltIsThreadInterrupted(&decoderThread)) {''')
    audio = replace(audio, '            // An exit signal was received\n            return;', '            // An exit signal was received\n            break;')
    audio = replace(audio, '        free(packet);\n    }\n}\n\nvoid stopAudioStream', '        free(packet);\n    }\n    QaReport(LI_AUDIO_STARTUP_DECODE_EXITED, 0, 0);\n}\n\nvoid stopAudioStream')
    audio = replace(audio, 'void stopAudioStream(void) {', 'void stopAudioStream(void) {\n    QaReport(LI_AUDIO_STARTUP_STOPPING, 0, 0);')
    audio = replace(audio, '    if (err != 0) {\n        return err;\n    }\n\n    AudioCallbacks.start();', '    if (err != 0) {\n        QaReport(0, err, 0);\n        return err;\n    }\n\n    AudioCallbacks.start();')
    audio = replace(audio, '''    if (err != 0) {
        AudioCallbacks.stop();
        closeSocket(rtpSocket);
        AudioCallbacks.cleanup();''', '''    if (err != 0) {
        QaReport(0, err, 0);
        AudioCallbacks.stop();
        // destroyAudioStream owns the socket and ping thread on rollback.
        AudioCallbacks.cleanup();''')
    audio = replace(audio, '''        if (err != 0) {
            AudioCallbacks.stop();''', '''        if (err != 0) {
            QaReport(0, err, 0);
            AudioCallbacks.stop();''')
    audio = replace(audio, '''            closeSocket(rtpSocket);
            AudioCallbacks.cleanup();''', '''            // destroyAudioStream owns the socket and ping thread on rollback.
            AudioCallbacks.cleanup();''')
    audio = replace(audio, '    return 0;\n}\n\nint LiGetPendingAudioFrames', '    QaReport(LI_AUDIO_STARTUP_START_COMMITTED, 0, 0);\n    return 0;\n}\n\nint LiGetPendingAudioFrames')
    changes['AudioStream.c'] = audio
    chunks = []
    for name, updated in changes.items():
        original = (base / 'src' / name).read_text(encoding='utf-8')
        chunks.extend(difflib.unified_diff(original.splitlines(True), updated.splitlines(True),
            fromfile='a/src/' + name, tofile='b/src/' + name))
    patch.write_text(''.join(chunks), encoding='utf-8', newline='\n')

if out.exists() and '--generate' in sys.argv:
    assert previous_patch is not None
    subprocess.run(['git', 'apply', '--reverse', '--check', '-'], cwd=out,
                   input=previous_patch.encode(), check=True)
    subprocess.run(['git', 'apply', '--reverse', '-'], cwd=out,
                   input=previous_patch.encode(), check=True)
    subprocess.run(['git', 'apply', '--check', '-'], cwd=out,
                   input=patch.read_text(encoding='utf-8').encode(), check=True)
    subprocess.run(['git', 'apply', '-'], cwd=out,
                   input=patch.read_text(encoding='utf-8').encode(), check=True)
elif out.exists():
    subprocess.run(['git', 'apply', '--reverse', '--check', '-'], cwd=out,
                   input=patch.read_text(encoding='utf-8').encode(), check=True)
else:
    shutil.copytree(base, out)
    subprocess.run(['git', 'apply', '--check', '-'], cwd=out,
                   input=patch.read_text(encoding='utf-8').encode(), check=True)
    subprocess.run(['git', 'apply', '-'], cwd=out,
                   input=patch.read_text(encoding='utf-8').encode(), check=True)
shutil.copy2(overlay / 'quest3d/AudioStartup.h', out / 'src/AudioStartup.h')
print(out)
