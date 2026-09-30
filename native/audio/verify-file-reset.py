"""Real private IPC/Opus lifetime probe; no live host, audio device, UDP or Quest."""
from pathlib import Path
from fractions import Fraction
import ctypes
import hashlib
import json
import queue
import secrets
import subprocess
import threading
import time

import av
import numpy as np

from quest3d.file_audio import FileAudioLink
from quest3d.file_transport_reset import FileResetBinding, FileTransportReset
from quest3d.media_playout import FileAVPlayback

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "native/audio/dist/file_reset_probe.exe"
OLD_PROBE = ROOT / "native/audio/dist/pcm_bridge_probe.exe"


def make_source(path):
    """Generated real A/V file with exact source PTS, stereo PCM and flat video."""
    packets = []
    with av.open(str(path), "w", format="nut") as output:
        video = output.add_stream("ffv1", rate=10)
        video.width, video.height, video.pix_fmt = 32, 16, "bgra"
        video.time_base = video.codec_context.time_base = Fraction(1, 10)
        audio = output.add_stream("pcm_f32le", rate=48000)
        audio.layout = "stereo"
        audio.time_base = audio.codec_context.time_base = Fraction(1, 48000)
        for index in range(20):
            image = np.full((16, 32, 4), (index * 10, 20, 50, 255), np.uint8)
            frame = av.VideoFrame.from_ndarray(image, format="bgra")
            frame.pts, frame.time_base = index, Fraction(1, 10)
            packets.extend(video.encode(frame))
        packets.extend(video.encode())
        for index in range(200):
            t = np.arange(index * 480, (index + 1) * 480) / 48000
            samples = np.stack((.25 * np.sin(t * 2 * np.pi * 440), .125 * np.cos(t * 2 * np.pi * 997)), axis=1).astype(np.float32)
            frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="flt", layout="stereo")
            frame.sample_rate, frame.time_base, frame.pts = 48000, Fraction(1, 48000), index * 480
            packets.extend(audio.encode(frame))
        packets.extend(audio.encode())
        for packet in sorted(packets, key=lambda p: (p.dts * p.time_base, p.stream.index)):
            output.mux(packet)


def wait_for(predicate, timeout=3):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        value = predicate()
        if value: return value
        time.sleep(.002)
    raise TimeoutError("Actual private probe event was not observed")


def consumer_mutex_present(publisher):
    """Observe object existence without acquiring or retaining ownership."""
    ctypes.set_last_error(0)
    handle = publisher.win.k.OpenMutexW(0x100000, False, "Local\\Quest3D.Audio.v1." + publisher.channel + ".Consumer")
    if handle:
        publisher.win.k.CloseHandle(handle)
        return True
    if ctypes.get_last_error() != 2:
        raise ctypes.WinError(ctypes.get_last_error())
    return False


def run_case(source, directory, operation):
    observed, events = [], queue.Queue()
    with FileAVPlayback(source) as player:
        wait_for(player.ready_for_start)
        link = FileAudioLink(player)
        child = subprocess.Popen([str(PROBE), link.publisher.channel], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, encoding="utf-8")
        def collect():
            for line in child.stdout:
                value = json.loads(line)
                observed.append({"received_ns": time.perf_counter_ns(), **value})
                events.put(value)
        reader = threading.Thread(target=collect)
        reader.start()
        def receive(name):
            value = events.get(timeout=3)
            assert value["event"] == name, value
            return value
        def send(command):
            child.stdin.write(command + "\n")
            child.stdin.flush()
        try:
            opened = receive("reader_open")
            initial = link.refresh()
            assert initial["consumer_pid"] == child.pid == opened["pid"]
            assert initial["consumer_creation_filetime"] == int(opened["creation_filetime"])
            player.start()
            link.adopt_started_clock()
            state = link.refresh()
            clock = player.clock_snapshot()
            # Certificate and transport are explicit fixture identifiers, not paired credentials.
            binding = FileResetBinding(player.session_id, link.publisher.channel, secrets.token_hex(16), "d"*64,
                state["producer_pid"], state["producer_creation_filetime"], child.pid,
                state["consumer_creation_filetime"], clock.epoch, clock.generation)
            reset = FileTransportReset(link, binding)
            link.pump()
            receive("gate_held")
            body = {"version": 1, "request_id": secrets.token_hex(16), "expected_revision": "0",
                    "file_session_id": player.session_id, "old_transport_epoch": binding.transport_epoch,
                    "operation": operation, "paused": operation == "pause"}
            if operation == "seek": body["seek_ns"] = "900000000"
            begin = time.perf_counter_ns()
            ticket = reset.request(body)
            assert reset.poll_local_flush() is None  # Actual send gate remains held.
            assert not reset.snapshot()["local_encoder_flush_confirmed"]
            before_release = time.perf_counter_ns()
            send("release_send")
            receive("local_flush")
            final = wait_for(reset.poll_local_flush)
            after_flush = time.perf_counter_ns()
            assert final.encoded_samples > 0 and final.transport_reset_required and not final.quest_queue_flushed
            assert consumer_mutex_present(link.publisher) and child.poll() is None
            assert player.clock_snapshot().position(time.perf_counter_ns()) == ticket.frozen_position_ns
            send("retire")
            retired = receive("fixture_retired")
            after_retire = time.perf_counter_ns()
            assert retired["passed"] and retired["reader_retired"] and retired["consumer_mutex_gone"]
            assert retired["packet_gate_closed"] and not retired["actual_host_session_closed"]
            assert child.poll() is None and not consumer_mutex_present(link.publisher)
            duplicate = subprocess.run([str(OLD_PROBE), link.publisher.channel, "50", "480", str(directory / (operation + "-unused.f32"))],
                                       capture_output=True, text=True, timeout=3)
            assert duplicate.returncode == 3 and json.loads(duplicate.stdout)["opened"] is False
            assert not consumer_mutex_present(link.publisher)
            try:
                reset.commit()
                raise AssertionError("A diagnostic fixture cannot authorize epoch restart")
            except RuntimeError as error:
                assert "not connected" in str(error)
            assert player.clock_snapshot().epoch == binding.media_epoch
            send("exit")
            assert child.wait(3) == 0
            reader.join(3)
            assert not reader.is_alive()
            result = {"operation": operation, "actual_native_pid": child.pid, "channel": link.publisher.channel,
                      "source_scope": [player.session_id, binding.media_epoch, binding.media_generation],
                      "frozen_position_ns": ticket.frozen_position_ns, "requested_seek_ns": ticket.request.seek_ns,
                      "actual_encoder_samples": final.encoded_samples, "actual_encoder_packets": final.encoded_packets,
                      "freeze_to_blocked_observation_ms": (before_release - begin) / 1e6,
                      "gate_release_to_local_flush_ms": (after_flush - before_release) / 1e6,
                      "local_flush_to_reader_retired_ms": (after_retire - after_flush) / 1e6,
                      "old_channel_reopen_rejected": True, "normal_child_exit": True,
                      "state": reset.snapshot(), "events": observed}
            return result
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(3)  # Failure cleanup only; no pass result is produced.
            reader.join(3)
            child.stdin.close(); child.stdout.close(); child.stderr.close()
            link.close()


def main():
    directory = ROOT / "artifacts/audio" / ("file-reset-" + time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3))
    directory.mkdir()
    make_source(directory / "generated-av.nut")
    summary = {"scope": "private fixture; not production transport reset", "passed": False,
               "native_sha256": hashlib.sha256(PROBE.read_bytes()).hexdigest(),
               "source_sha256": hashlib.sha256((ROOT / "native/audio/file_reset_probe.cpp").read_bytes()).hexdigest(),
               "windows_audio_changed": False, "network_used": False, "quest_audio_verified": False, "cases": []}
    try:
        for operation in ("pause", "seek", "replay"):
            summary["cases"].append(run_case(directory / "generated-av.nut", directory, operation))
        assert len({item["channel"] for item in summary["cases"]}) == 3
        summary["passed"] = True
    except Exception as error:
        summary["failure"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        (directory / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps({"directory": str(directory), "passed": summary["passed"], "cases": len(summary["cases"])}))


if __name__ == "__main__": main()
