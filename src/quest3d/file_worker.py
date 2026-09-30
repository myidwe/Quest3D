"""Host-owned file preparation and PCM export over private inherited pipes.

No public listener, common desktop FramePublisher or remote READY is created.
The parent must verify the real worker process within its owned job, bind the
actual stream owner, validate device progress and control the presentation gate.
"""
import argparse
from fractions import Fraction
import hashlib
import os
from pathlib import Path
import sys

from .file_worker_protocol import (MAX_LINE_BYTES, MAX_SEQUENCE, decode_request, encode_message,
                                   fields, identifier, unsigned_decimal)


class PreparedFileWorker:
    """One selected file, with a fixed private parent and export scope.

    Calls are serialized by the control actor. The media decoder and optional
    real depth worker keep their existing bounded queues. IPC handoff never
    acknowledges player audio or starts the media clock.
    """
    def __init__(self, worker_nonce, transport, output):
        self.worker_nonce, self.transport = identifier(worker_nonce), identifier(transport)
        self.output = Path(output)
        self.player = self.ai = self.publisher = self.scope = None
        self.closed = self.closing = False
        self.initial_seek_ns = None
        self.mode = "2d"
        self.sequence = 0
        self.exported = {}
        self.eof_sent = False
        self.last_original_end_pts = None
        self.last_original_block_id = None

    def dispatch(self, op, data):
        if self.closed and op not in ("describe", "close"):
            raise RuntimeError("File worker has closed")
        if self.closing and op not in ("describe", "close"):
            raise RuntimeError("File worker cleanup is pending; only describe/close may run")
        handler = {"open": self.open_file, "describe": self.describe, "prepare": self.prepare,
                   "bind_audio": self.bind_audio, "pump": self.pump,
                   "release": self.release, "close": self.close_command}.get(op)
        if handler is None:
            raise ValueError("Unsupported file worker operation")
        return handler(data)

    def open_file(self, data):
        fields(data, ("path",), ("mode", "seek_ns"))
        if self.player is not None:
            raise RuntimeError("This worker already owns a selected file")
        raw_path = data["path"]
        if type(raw_path) is not str or not 0 < len(raw_path) <= 4096 or "\0" in raw_path:
            raise ValueError("Invalid local file path")
        path = Path(raw_path)
        if not path.is_absolute() or not path.is_file():
            raise ValueError("Expected an existing absolute local file path")
        mode = data.get("mode", "2d")
        if type(mode) is not str or mode not in ("2d", "3d"):
            raise ValueError("Invalid file display mode")
        seek_ns = unsigned_decimal(data["seek_ns"], "seek time") if "seek_ns" in data else None
        # Delay heavyweight/library imports until stdout has been redirected to stderr.
        from .media_playout import FileAVPlayback
        self.initial_seek_ns = seek_ns
        try:
            self.player = FileAVPlayback(path, paused=False, lookahead_ms=120)
            self.player.__enter__()
            self.mode = mode
            if mode == "3d":
                from .file_session import FileAIWorker
                self.output.mkdir(parents=True, exist_ok=False)
                self.ai = FileAIWorker(self.player, eye_width=1280, eye_height=720, ai_size=280,
                                       directory=self.output / "ai", mode="3d", disparity=12)
            return self.describe({})
        except BaseException:
            self.close()
            raise

    def _require_file(self):
        if self.player is None:
            raise RuntimeError("No file has been opened")

    def describe(self, data):
        fields(data)
        if self.player is None and self.closing:
            return dict(closing=self.closing, closed=self.closed, video_publication_active=False,
                        quest_ready_verified=False)
        self._require_file()
        status = self.player.status()
        clock = self.player.clock_snapshot()
        ai = self.ai.snapshot() if self.ai else None
        return dict(file_session=clock.session_id, epoch=str(clock.epoch), generation=str(clock.generation),
                    mode=self.mode, metadata_ready=status["metadata_ready"],
                    preroll_ready=self.player.ready_for_start(require_ai=self.mode == "3d"),
                    preroll=clock.preroll, paused=clock.paused, audio_frames_buffered=status["audio_samples"],
                    has_audio=self.player.has_audio, audio_eof=status["audio_eof"],
                    audio_required=self.player.has_audio and not (status["audio_eof"] and status["audio_samples"] == 0),
                    audio_frames_consumed=status["audio_consumed_samples"],
                    error=status["error"], ai=ai, audio_bound=self.scope.request() if self.scope else None,
                    exported_records=self.sequence, eof_exported=self.eof_sent,
                    video_publication_active=False, quest_ready_verified=False,
                    closing=self.closing, closed=self.closed)

    def prepare(self, data):
        fields(data)
        self._require_file()
        if self.initial_seek_ns is not None:
            if not self.player.status()["metadata_ready"]:
                raise RuntimeError("File metadata is not ready for initial seek")
            self.player.seek(self.initial_seek_ns, paused=False)
            self.initial_seek_ns = None
        clock = self.player.prepare_native_epoch(require_ai=self.mode == "3d")
        return dict(file_session=clock.session_id, epoch=str(clock.epoch), generation=str(clock.generation),
                    prepared=True, media_clock_released=False, quest_ready_verified=False)

    def bind_audio(self, data):
        fields(data, ("file_session", "epoch", "generation", "channel"))
        self._require_file()
        if self.publisher is not None:
            raise RuntimeError("This native epoch already has an audio publisher")
        status = self.player.status()
        if not status["metadata_ready"]:
            raise RuntimeError("File metadata is not ready")
        if not self.player.has_audio or (status["audio_eof"] and status["audio_samples"] == 0):
            raise RuntimeError("This file epoch has no remaining PCM; skip the audio binding")
        from .file_audio_protocol import Scope
        from .file_audio_ipc import FileAudioIPCPublisher
        scope = Scope(data["file_session"], self.transport, data["channel"],
                      unsigned_decimal(data["epoch"], "epoch"), unsigned_decimal(data["generation"], "generation"))
        clock = self.player.clock_snapshot()
        if (scope.file_session, scope.epoch, scope.generation) != (clock.session_id, clock.epoch, clock.generation):
            raise ValueError("Audio grant does not match the prepared file epoch")
        # native_audio_batch is the player's checked preparation boundary; it
        # issues original tokens but never consumes them or advances the clock.
        self.player.native_audio_batch(max_packets=1)
        self.publisher = FileAudioIPCPublisher(scope)
        self.scope = scope
        return dict(scope=scope.request(), ipc=self.publisher.snapshot(), quest_ready_verified=False)

    def pump(self, data):
        fields(data)
        if self.publisher is None:
            raise RuntimeError("No exact audio epoch has been bound")
        from .file_audio_protocol import from_scheduled, eof
        state = self.publisher.snapshot()
        if state["consumer_closed"]:
            raise RuntimeError("The exact native audio consumer has stopped")
        if not state["consumer_attached"]:
            return dict(accepted=0, waiting_for_native_consumer=True, ipc=state)
        packets, final = self.player.native_audio_batch(max_packets=50)
        accepted, all_exported = 0, True
        for packet in packets:
            token = packet.token
            raw = packet.samples.astype("<f4", copy=False).tobytes()
            stamp = (packet.offset_samples, packet.total_samples, packet.pts_ns, hashlib.sha256(raw).digest())
            if token in self.exported:
                # No player ACK path is wired yet, so exact repeated pending
                # suffixes must remain immutable and must not gain new sequence.
                if self.exported[token] != stamp:
                    raise RuntimeError("Already exported original PCM suffix changed")
                continue
            if self.eof_sent:
                raise RuntimeError("New original PCM appeared after exported EOF")
            if len(self.exported) >= 50:
                raise RuntimeError("Unacknowledged PCM export ledger is full")
            block = from_scheduled(self.scope, self.sequence, packet)
            if not self.publisher.offer(block):
                all_exported = False
                break
            self.exported[token] = stamp
            self.sequence += 1
            accepted += 1
            self.last_original_block_id = block.block_id
            end_absolute = packet.absolute_pts + Fraction(packet.total_samples - packet.offset_samples, 48000)
            self.last_original_end_pts = round((end_absolute - self.player.clock_snapshot().video_origin) * 1_000_000_000)
        if all_exported and final and not self.eof_sent and self.last_original_block_id is not None:
            block = eof(self.scope, self.sequence, self.last_original_block_id + 1, self.last_original_end_pts)
            if self.publisher.offer(block):
                self.eof_sent = True
                self.sequence += 1
                accepted += 1
        return dict(accepted=accepted, pending_original_blocks=len(self.exported), eof_exported=self.eof_sent,
                    ipc=self.publisher.snapshot(), device_consumption_verified=False,
                    audio_frames_consumed=self.player.metrics["audio_consumed_samples"])

    def release(self, data):
        fields(data, ("file_session", "epoch", "generation", "start_host_ns"))
        self._require_file()
        # The actual parent coordinator must acquire Quest/device/video start
        # evidence first. This primitive only releases the prepared common clock.
        exact = (data["file_session"], unsigned_decimal(data["epoch"], "epoch"),
                 unsigned_decimal(data["generation"], "generation"))
        self.player.release_native_epoch(exact, unsigned_decimal(data["start_host_ns"], "host start time"))
        return dict(media_clock_released=True, **self.describe({}))

    def close_command(self, data):
        fields(data)
        self.close()
        return dict(closed=True, process_exit_pending=True, quest_audio_retirement_verified=False)

    def close(self):
        # Do not report close while an actual child resource remains live. A
        # failed cleanup keeps references so the parent can request another close.
        self.closing = True
        if self.publisher is not None:
            self.publisher.close()
            self.publisher = None
        if self.ai is not None:
            self.ai.close()
            self.ai = None
        if self.player is not None:
            self.player.__exit__(None, None, None)
            self.player = None
        self.closed = True


def run_control(input_stream, output_stream, worker):
    from .audio_bridge import _Windows
    win = _Windows()
    output_stream.write(encode_message(dict(v=1, type="hello", worker_nonce=worker.worker_nonce,
        pid=os.getpid(), creation_filetime=str(win.creation(win.k.GetCurrentProcess())))))
    output_stream.flush()
    expected = 1
    try:
        while expected <= MAX_SEQUENCE:
            line = input_stream.readline(MAX_LINE_BYTES + 2)
            if not line:
                return 0
            request = decode_request(line, worker.worker_nonce, expected)
            expected += 1
            reply = dict(v=1, type="reply", worker_nonce=worker.worker_nonce, seq=request["seq"])
            try:
                reply.update(ok=True, result=worker.dispatch(request["op"], request["data"]))
            except (ValueError, RuntimeError, OSError) as error:
                reply.update(ok=False, error=type(error).__name__, message=str(error)[:400])
            output_stream.write(encode_message(reply))
            output_stream.flush()
            if worker.closed:
                return 0
        raise ValueError("Control sequence exhausted")
    finally:
        worker.close()


def isolate_control_stdout():
    """Retain private control output while directing Python/native stdout to logs."""
    control_fd = os.dup(sys.stdout.fileno())
    os.set_inheritable(control_fd, False)
    try:
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno(), inheritable=False)
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            import msvcrt
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
            kernel.SetStdHandle.restype = wintypes.BOOL
            if not kernel.SetStdHandle(wintypes.DWORD(-11), msvcrt.get_osfhandle(sys.stdout.fileno())):
                raise ctypes.WinError(ctypes.get_last_error())
        sys.stdout = sys.stderr
        return os.fdopen(control_fd, "wb", buffering=MAX_LINE_BYTES + 1)
    except BaseException:
        os.close(control_fd)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-nonce", required=True)
    parser.add_argument("--transport", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    from .paths import ARTIFACT_DIR
    output = args.output or ARTIFACT_DIR / ("file-worker-" + identifier(args.worker_nonce))
    if not output.is_absolute():
        parser.error("Output directory must be absolute")
    control_out = isolate_control_stdout()
    worker = PreparedFileWorker(args.worker_nonce, args.transport, output)
    try:
        return run_control(sys.stdin.buffer, control_out, worker)
    except (ValueError, OSError, RuntimeError) as error:
        print(f"File worker stopped: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        return 2
    finally:
        control_out.close()


if __name__ == "__main__":
    raise SystemExit(main())
