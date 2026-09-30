"""Actual child-process PCM bridge/Opus checks. Never opens an audio endpoint."""
from pathlib import Path
import argparse
from fractions import Fraction
import json
import os
import secrets
import subprocess
import sys
import time
import threading
import ctypes

import numpy as np

from quest3d.audio_bridge import AudioPublisher, _Windows
from quest3d.media_playout import ScheduledPCM

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "native/audio/dist/pcm_bridge_probe.exe"
SESSION = "c" * 32


def wait(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value: return value
        time.sleep(.002)
    raise TimeoutError("Private PCM fixture did not reach expected state")


def offer_signal(bridge, blocks=20, *, epoch=0, generation=0, first_id=0):
    signal = np.arange(blocks * 480, dtype=np.float64) / 48000
    samples = np.stack((.25 * np.sin(signal * 2 * np.pi * 1000), .15 * np.sin(signal * 2 * np.pi * 1500)), axis=1).astype(np.float32)
    due = time.perf_counter_ns() + 200_000_000
    for i in range(blocks):
        packet = ScheduledPCM((SESSION, epoch, generation, first_id + i), samples[i * 480:(i + 1) * 480],
            i * 10_000_000, Fraction(i, 100), due + i * 10_000_000, 0, 480, None)
        assert bridge.offer(packet)
    return samples


def signal_check(path, expected):
    decoded = np.fromfile(path, dtype=np.float32).reshape(-1, 2)
    assert decoded.shape == expected.shape, (decoded.shape, expected.shape)
    scores = []
    for channel in (0, 1):
        scores.append(max(float(np.corrcoef(expected[500:8000, channel], decoded[500 + lag:8000 + lag, channel])[0, 1]) for lag in range(241)))
    assert min(scores) > .98, scores
    return {"decoded_samples": len(decoded), "channel_correlation_with_codec_delay": scores}


def terminate_test_producer(identity):
    """Only the exact PID/creation reported by this script's child fixture is killed.

    A Windows venv python.exe may be a launcher, so Popen.pid is not assumed to
    be the process which owns the mapping. Never close stdin before owner death.
    """
    win = _Windows()
    process = win.k.OpenProcess(0x100000 | 0x1000 | 1, False, identity["pid"])
    assert process and win.creation(process) == identity["creation"]
    try:
        win.k.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        win.k.TerminateProcess.restype = ctypes.c_int
        assert win.k.TerminateProcess(process, 91)
        assert win.k.WaitForSingleObject(process, 3000) == 0
    finally:
        win.k.CloseHandle(process)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--producer-child")
    parser.add_argument("--production", action="store_true")
    args = parser.parse_args()
    if args.producer_child:
        with AudioPublisher(SESSION, channel=args.producer_child) as bridge:
            print(json.dumps({"ready": True, "pid": os.getpid(),
                              "creation": bridge.win.creation(bridge.win.k.GetCurrentProcess())}), flush=True)
            sys.stdin.readline()
        return
    directory = ROOT / "artifacts/audio" / ("pcm-bridge-" + time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3))
    directory.mkdir()
    results = []
    if args.production:
        with AudioPublisher(SESSION) as bridge:
            output = directory / "production.f32"
            env = dict(os.environ, QUEST3D_FILE_AUDIO_CHANNEL=bridge.channel, QUEST3D_PCM_OUTPUT=str(output),
                       GCOV_PREFIX=str(directory / "coverage"))
            build = ROOT / "third_party/sunshine/cmake-build-quest3d"
            with (directory / "production.log").open("w") as log:
                child = subprocess.Popen([str(build / "tests/test_sunshine.exe"), "--gtest_filter=Quest3DFileAudio.RealPublisherThroughProductionOpus",
                    "--gtest_output=xml:" + str(directory / "production.xml")], cwd=build, env=env, stdout=log, stderr=subprocess.STDOUT)
                try:
                    wait(lambda: bridge.snapshot()["consumer_ready"])
                    bridge.prepare_scope(0, 1)
                    expected = offer_signal(bridge, generation=1)
                    bridge.mark_decoder_eof()
                    assert child.wait(7) == 0
                    assert sum(a.consumed_samples for a in bridge.poll_acks()) == 9600
                    assert bridge.snapshot()["encoder_consumed_samples"] == 9600
                    results.append({"name": "production_entry_opus", **signal_check(output, expected)})
                finally:
                    if child.poll() is None: child.terminate(); child.wait(3)
    else:
        with AudioPublisher(SESSION) as bridge:
            output = directory / "probe.f32"
            child = subprocess.Popen([str(PROBE), bridge.channel, "1800", "240", str(output)], stdout=subprocess.PIPE, text=True)
            try:
                wait(lambda: bridge.snapshot()["consumer_ready"])
                duplicate = subprocess.run([str(PROBE), bridge.channel, "50", "240", str(directory / "duplicate.f32")], capture_output=True, text=True)
                assert duplicate.returncode == 3
                bridge.prepare_scope(0, 1)
                expected = offer_signal(bridge, generation=1)
                bridge.mark_decoder_eof()
                wait(lambda: bridge.snapshot()["encoder_consumed_samples"] == 9600)
                acks = bridge.poll_acks()
                assert sum(a.consumed_samples for a in acks) == 9600
                sequence = bridge.begin_flush()
                final = bridge.finish_flush(sequence)
                assert final.encoded_samples == 9600 and final.transport_reset_required and not final.quest_queue_flushed
                bridge.confirm_acks(acks)
                bridge.resume(0, 2)
                bridge.prepare_scope(0, 3)
                assert bridge.snapshot()["generation"] == 3 and bridge.snapshot()["buffered_samples"] == 0
                # A resumed scope cannot be populated with a stale media generation.
                child_output = child.communicate(timeout=4)[0]
                assert child.returncode == 0, child_output
                results.append({"name": "actual_ipc_duplicate_flush_scope", **json.loads(child_output), **signal_check(output, expected)})
            finally:
                if child.poll() is None: child.terminate(); child.wait(3)
        with AudioPublisher(SESSION) as bridge:
            child = subprocess.Popen([str(PROBE), bridge.channel, "3000", "480", str(directory / "consumer-death.f32")], stdout=subprocess.PIPE, text=True)
            wait(lambda: bridge.snapshot()["consumer_ready"])
            child.terminate(); child.communicate(timeout=3)
            assert bridge.snapshot()["fault"] == "consumer_lost"
            assert not bridge.poll_acks()
            results.append({"name": "exact_consumer_death", "passed": True})
        channel = secrets.token_hex(16)
        producer = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--producer-child", channel], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        identity = json.loads(producer.stdout.readline())
        assert identity["ready"]
        consumer = subprocess.Popen([str(PROBE), channel, "800", "240", str(directory / "producer-death.f32")], stdout=subprocess.PIPE, text=True)
        try:
            time.sleep(.2)
            terminate_test_producer(identity); producer.communicate(timeout=3)
            output = consumer.communicate(timeout=3)[0]
            state = json.loads(output)
            assert consumer.returncode == 5 and state["failed_steps"] > 0 and state["packets"] == 0, state
            results.append({"name": "exact_producer_death", "passed": True})
        finally:
            for process in (producer, consumer):
                if process.poll() is None: process.terminate(); process.wait(3)
        with AudioPublisher(SESSION) as bridge:
            thread = threading.Thread(target=lambda: bridge.win.k.WaitForSingleObject(bridge.mutex, 50))
            thread.start(); thread.join()
            child = subprocess.Popen([str(PROBE), bridge.channel, "100", "240", str(directory / "abandoned-open.f32")],
                env=dict(os.environ, QUEST3D_PCM_PROBE_HOLD_FAILED="1"), stdout=subprocess.PIPE, text=True)
            try:
                assert json.loads(child.stdout.readline())["opened"] is False
                # Failed native open remains alive here; it must have released acquired ownership.
                assert child.poll() is None
                assert bridge.snapshot()["fault"] == "abandoned_mutex"
                assert child.wait(2) == 3
                results.append({"name": "abandoned_open_releases_mutex_before_waiting", "passed": True})
            finally:
                if child.poll() is None: child.terminate(); child.wait(3)
        channel = secrets.token_hex(16)
        producer = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--producer-child", channel], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        assert json.loads(producer.stdout.readline())["ready"]
        consumer = subprocess.Popen([str(PROBE), channel, "800", "240", str(directory / "normal-owner-exit.f32")], stdout=subprocess.PIPE, text=True)
        try:
            time.sleep(.2)
            producer.communicate(input="stop\n", timeout=3)
            state = json.loads(consumer.communicate(timeout=3)[0])
            assert producer.returncode == consumer.returncode == 0 and state["failed_steps"] == 0
            results.append({"name": "normal_producer_exit_stops_without_ghost_consumption", "passed": True})
        finally:
            for process in (producer, consumer):
                if process.poll() is None: process.terminate(); process.wait(3)
    summary = {"tests": len(results), "results": results, "actual_windows_audio_changed": False,
               "actual_network_used": False, "quest_audio_verified": False, "directory": str(directory)}
    (directory / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary))


if __name__ == "__main__": main()
