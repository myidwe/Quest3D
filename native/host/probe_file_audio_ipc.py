"""Real Windows Python-to-native PCM IPC; private file handoff is not playback.

The CLI flushes owned wire bytes to a test file before committing a handoff.
It supplies no authenticated launch, Sunshine broker, audio device or AV ACK.
"""
from dataclasses import replace
from datetime import datetime
from ctypes import wintypes as W
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid

from quest3d.audio_bridge import _Windows
from quest3d.file_audio_ipc import FileAudioIPCPublisher, HEADER_BYTES, U32
from quest3d.file_audio_protocol import Scope, Block, encode, eof
from quest3d.media_audio import MediaAudioReader

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "artifacts/host/cmake-build-file-audio-ipc/tests/file_audio_ipc_reader_cli.exe"
CLI_SHA = "25a2e64c4a43cdd9f486e35a76794697a008e8f5966cbede2236305253e77d3e"
VECTORS = ROOT / "artifacts/audio/file-audio-vectors-20260910-a"
SOURCE = VECTORS / "known-av.nut"
TRACKED = [Path(__file__), CLI, SOURCE, VECTORS / "manifest.json", VECTORS / "first.pcm",
           VECTORS / "seek.pcm", ROOT / "src/quest3d/file_audio_ipc.py",
           ROOT / "src/quest3d/file_audio_protocol.py", ROOT / "src/quest3d/audio_bridge.py",
           ROOT / "src/quest3d/media_audio.py", ROOT / "native/host/file_audio_ipc_reader_cli.cpp",
           ROOT / "third_party/sunshine/src/quest3d_file_audio_ipc.h",
           ROOT / "third_party/sunshine/src/platform/windows/quest3d_file_audio_ipc_reader.h"]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def new_scope(epoch=7, generation=9):
    # Explicit fixture identifiers, never asserted to authorize a real launch.
    return Scope(*(uuid.uuid4().hex for _ in range(3)), epoch, generation)


def decoded_blocks(scope, seek=False):
    blocks, rows, final_pts = [], [], None
    with MediaAudioReader(SOURCE) as reader:
        if seek:
            reader.seek(20_000_000, scope.epoch)
        while True:
            try:
                pcm = reader.next()
            except StopIteration:
                break
            block = Block(scope, len(blocks), len(blocks), pcm.pts_ns, len(pcm.samples), 0,
                          pcm.samples, discontinuity=pcm.discontinuity)
            blocks.append(block)
            rows.append(dict(sequence=block.sequence, block_id=block.block_id, pts_ns=block.pts_ns,
                             count=block.count, total_samples=block.total_samples,
                             offset_samples=block.offset_samples, discontinuity=block.discontinuity))
            final_pts = pcm.end_pts_ns
    pcm_bytes = b"".join(b.samples.astype("<f4", copy=False).tobytes() for b in blocks)
    assert pcm_bytes == (VECTORS / ("seek.pcm" if seek else "first.pcm")).read_bytes()
    assert blocks[-1].count == 203
    assert sum(b.count for b in blocks) == (4043 if seek else 5003)
    golden = next(case for case in json.loads((VECTORS / "manifest.json").read_text())["cases"]
                  if case["name"] == ("seek" if seek else "first"))
    assert rows == [{key: row[key] for key in rows[0]} for row in golden["rows"]]
    assert final_pts == golden["final_pts_ns"] == 104229167
    blocks.append(eof(scope, len(blocks), len(blocks), final_pts))
    return blocks, pcm_bytes, rows, final_pts


class ProcessPeer:
    """Owned private child with bounded I/O and a joined non-daemon stdout reader."""
    def __init__(self, command, directory, name):
        self.rows, self.lines = [], queue.Queue()
        self.log_path = directory / (name + ".json")
        self.stderr = (directory / (name + ".stderr.txt")).open("w", encoding="utf-8")
        env = dict(os.environ)
        env["PATH"] = str(ROOT / "native/host/tools/msys64/ucrt64/bin") + os.pathsep + env.get("PATH", "")
        self.process = subprocess.Popen([str(c) for c in command], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=self.stderr, text=True, encoding="utf-8",
            env=env, cwd=ROOT, creationflags=subprocess.CREATE_NO_WINDOW)
        def read_lines():
            try:
                for line in self.process.stdout:
                    self.lines.put(line)
            finally:
                self.lines.put(None)
        self.thread = threading.Thread(target=read_lines, name="private-ipc-stdout")
        self.thread.start()
        self.forced = False
        self.expected_kill = False
        self.actual_process = None
        self.actual_pid = None
        self.win = _Windows()
        self.win.k.TerminateProcess.argtypes = [W.HANDLE, W.UINT]
        self.win.k.TerminateProcess.restype = W.BOOL

    def retain_actual_producer(self, pid, birth):
        assert self.actual_process is None and pid != os.getpid()
        handle = self.win.k.OpenProcess(0x100000 | 0x1000 | 1, False, pid)
        assert handle
        try:
            assert self.win.creation(handle) == birth
            assert self.win.k.WaitForSingleObject(handle, 0) == 0x102
        except BaseException:
            self.win.k.CloseHandle(handle)
            raise
        self.actual_process, self.actual_pid = handle, pid

    def receive(self, command):
        try:
            line = self.lines.get(timeout=8)
        except queue.Empty as error:
            raise TimeoutError(f"No native response for {command}") from error
        if line is None:
            raise RuntimeError(f"Private child ended before {command}")
        row = json.loads(line)
        assert row["command"] == command, row
        self.rows.append(dict(at=time.perf_counter_ns(), **row))
        return row

    def request(self, command):
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()
        return self.receive(command)

    def kill_for_test(self):
        self.expected_kill = True
        self.process.terminate()
        self.process.wait(timeout=5)

    def close(self, timeout=5):
        try:
            if not self.process.stdin.closed:
                self.process.stdin.close()  # Real EOF exits only this private CLI/producer.
            if self.process.poll() is None:
                try:
                    self.process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    self.forced = True
                    if self.actual_process and self.win.k.WaitForSingleObject(self.actual_process, 0) == 0x102:
                        assert self.win.k.TerminateProcess(self.actual_process, 74)
                    self.process.terminate()
                    self.process.wait(timeout=5)
            # A venv redirector can end before its child. The retained verified
            # actual producer handle is also waited/terminated, never just its PID.
            if self.actual_process and self.win.k.WaitForSingleObject(self.actual_process, int(timeout * 1000)) == 0x102:
                self.forced = True
                assert self.win.k.TerminateProcess(self.actual_process, 74)
                assert self.win.k.WaitForSingleObject(self.actual_process, 5000) == 0
            self.thread.join(5)
            assert not self.thread.is_alive(), "stdout reader failed to join"
        finally:
            if self.actual_process:
                assert self.win.k.CloseHandle(self.actual_process)
                self.actual_process = None
            for stream in (self.process.stdin, self.process.stdout, self.stderr):
                if not stream.closed:
                    stream.close()
            self.log_path.write_text(json.dumps(dict(pid=self.process.pid, rows=self.rows,
                exit_code=self.process.returncode, intentional_loss=self.expected_kill,
                verified_actual_producer_pid=self.actual_pid,
                forced_cleanup=self.forced, stdout_thread_joined=not self.thread.is_alive()), indent=2) + "\n")
        assert not self.forced, "Private child did not exit on EOF"
        if not self.expected_kill:
            assert self.process.returncode in (0, 4), self.process.returncode

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def native(scope, directory, name="native"):
    return ProcessPeer([CLI, scope.file_session, scope.transport, scope.channel, scope.epoch,
                        scope.generation, directory / (name + ".wire")], directory, name)


def bind(publisher, peer):
    opened = peer.receive("open")
    assert opened["ok"] and opened["producer_pid"] == os.getpid(), opened
    snapshot = publisher.snapshot()  # Actual OpenProcess + creation/liveness verification.
    assert snapshot["consumer_pid"] == peer.process.pid == opened["consumer_pid"]
    assert snapshot["consumer_creation_filetime"] == opened["consumer_creation"]
    assert snapshot["producer_creation_filetime"] == opened["producer_creation"]
    assert snapshot["consumer_attached"] and not snapshot["device_consumption_verified"]
    return opened, snapshot


def retire(peer, publisher=None):
    result = peer.request("retire")
    assert result["ok"] and result["stopped"] and result["resources_released"], result
    if publisher is not None:
        assert publisher.snapshot()["consumer_closed"]
    return result


def run_vector(directory, seek):
    scope = new_scope(8 if seek else 7, 10 if seek else 9)
    blocks, pcm, rows, end_pts = decoded_blocks(scope, seek)
    with FileAudioIPCPublisher(scope) as publisher, native(scope, directory) as peer:
        opened, _ = bind(publisher, peer)
        for block in blocks[:8]:
            assert publisher.offer(block)
        full = publisher.snapshot()
        assert full["queued_records"] == 8 and not publisher.offer(blocks[8])
        assert publisher.snapshot() == full
        for _ in range(3):
            peek = peer.request("peek")
            assert peek["ok"] and peek["head"] == 0 and peek["offered_sequence"] == 0
        assert publisher.snapshot() == full, "peek/full advanced the producer"
        next_offer = 8
        for sequence in range(len(blocks)):
            peek = peer.request("peek")
            assert peek["ok"] and peek["offered_sequence"] == sequence, peek
            committed = peer.request("accept")
            assert committed["ok"] and committed["head"] == sequence + 1, committed
            if next_offer < len(blocks):
                assert publisher.offer(blocks[next_offer])
                next_offer += 1
        snapshot = publisher.snapshot()
        assert snapshot["queued_records"] == 0 and snapshot["eof_handed_off"]
        assert snapshot["handed_off_frames"] == len(pcm) // 8
        assert snapshot["handed_off_records"] == len(blocks)
        closed = retire(peer, publisher)
    expected_wire = b"".join(encode(block) for block in blocks)
    actual_wire = (directory / "native.wire").read_bytes()
    assert actual_wire == expected_wire
    (directory / "expected.wire").write_bytes(expected_wire)
    (directory / "expected.pcm").write_bytes(pcm)
    return dict(scope=scope.request(), opened=opened, snapshot=snapshot, retirement=closed,
                frames=len(pcm) // 8, records=len(blocks), last_pcm_count=rows[-1]["count"],
                end_pts_ns=end_pts, rows=rows, pcm_sha256=digest(pcm), wire_sha256=digest(actual_wire),
                full_and_repeated_peek_no_advance=True, ring_wrapped=True)


def run_wrong_scope(directory):
    scope = new_scope()
    with FileAudioIPCPublisher(scope) as publisher:
        before = publisher.snapshot()
        with native(replace(scope, epoch=scope.epoch + 1), directory, "wrong") as peer:
            rejected = peer.receive("open")
            assert not rejected["ok"] and rejected["error"] == 9, rejected
        assert publisher.snapshot() == before
        with native(scope, directory) as peer:
            bind(publisher, peer)
            retire(peer, publisher)
    return dict(rejection=rejected, original_owner_unchanged=True, correct_open_after_rejection=True)


def run_duplicate(directory):
    scope = new_scope()
    with FileAudioIPCPublisher(scope) as publisher, native(scope, directory) as peer:
        bind(publisher, peer)
        before = publisher.snapshot()
        with native(scope, directory, "duplicate") as other:
            rejected = other.receive("open")
            assert not rejected["ok"] and rejected["error"] == 6, rejected
        assert publisher.snapshot() == before
        block = decoded_blocks(scope)[0][0]
        assert publisher.offer(block) and peer.request("peek")["ok"]
        assert peer.request("accept")["ok"]
        retire(peer, publisher)
    return dict(rejection=rejected, sole_consumer_preserved=True)


def run_changed_after_peek(directory, corrupt):
    scope = new_scope()
    with FileAudioIPCPublisher(scope) as publisher, native(scope, directory) as peer:
        bind(publisher, peer)
        block = decoded_blocks(scope)[0][0]
        assert publisher.offer(block) and peer.request("peek")["ok"]
        if corrupt:
            with publisher._lock():
                publisher.memory[HEADER_BYTES + 48 + 128] ^= 1
        else:
            publisher.close()
        rejected = peer.request("accept")
        assert not rejected["ok"] and not rejected["accepting"] and rejected["head"] == 0, rejected
        result = peer.request("retire")
        if corrupt:
            assert not result["ok"] and not result["resources_released"], result
        else:
            assert result["ok"] and result["resources_released"], result
    # The owned file write really happened. Its uncommitted bytes MUST NOT be retried.
    assert (directory / "native.wire").read_bytes() == encode(block)
    return dict(rejection=rejected, retirement=result, file_handoff_before_failed_commit=True,
                retry_prohibited=True, device_consumption_verified=False)


def run_mutex_retire(directory):
    scope = new_scope()
    with FileAudioIPCPublisher(scope) as publisher, native(scope, directory) as peer:
        bind(publisher, peer)
        assert publisher.offer(decoded_blocks(scope)[0][0])
        assert peer.request("peek")["ok"]
        # Main Python thread owns the actual named mutex; separate native process times out.
        assert publisher.win.k.WaitForSingleObject(publisher.mutex, 1000) == 0
        try:
            begin = time.perf_counter_ns()
            held = peer.request("retire")
            elapsed_ms = (time.perf_counter_ns() - begin) / 1e6
            assert not held["ok"] and not held["resources_released"] and not held["stopped"]
            assert held["error"] == 7 and held["windows_error"] == 1460, held
            assert publisher._get(144, U32) == 0
        finally:
            assert publisher.win.k.ReleaseMutex(publisher.mutex)
        done = retire(peer, publisher)
        snapshot = publisher.snapshot()
        assert snapshot["handed_off_frames"] == 0 and snapshot["queued_records"] == 1
    return dict(held=held, retry=done, elapsed_ms=elapsed_ms, pending_record_not_consumed=True)


def run_consumer_loss(directory):
    scope = new_scope()
    with FileAudioIPCPublisher(scope) as publisher, native(scope, directory) as peer:
        bind(publisher, peer)
        assert publisher.offer(decoded_blocks(scope)[0][0])
        assert peer.request("peek")["ok"]
        peer.kill_for_test()
        try:
            publisher.snapshot()
        except RuntimeError as error:
            assert "consumer_lost" in str(error), error
            failure = str(error)
        else:
            raise AssertionError("Dead native consumer accepted")
        with publisher._lock():
            assert publisher._get(112) == 0 and publisher._get(144, U32) == 0
    assert not (directory / "native.wire").read_bytes()
    return dict(actual_consumer_pid=peer.process.pid, expected_process_termination=True,
                failure=failure, handoff_advanced=False, close_receipt=False)


def producer_child(scope):
    with FileAudioIPCPublisher(scope) as publisher:
        assert publisher.offer(decoded_blocks(scope)[0][0])
        print(json.dumps(dict(command="producer_open", **publisher.snapshot())), flush=True)
        command = sys.stdin.readline().strip()
        if command == "crash":
            os._exit(73)  # Test actual producer loss without running context-manager cleanup.
        if command == "hang":
            print(json.dumps(dict(command="hang", producer_pid=os.getpid())), flush=True)
            threading.Event().wait()  # Deliberate private-child cleanup-fallback test only.


def run_producer_loss(directory):
    scope = new_scope()
    command = [sys.executable, Path(__file__), "--producer-child", json.dumps(scope.request())]
    with ProcessPeer(command, directory, "producer") as producer:
        opened = producer.receive("producer_open")
        # Windows venv python.exe can be a redirector parent. Verify the actual
        # mapping producer from a real process handle, rather than its launcher PID.
        producer.retain_actual_producer(opened["producer_pid"], opened["producer_creation_filetime"])
        with native(scope, directory) as peer:
            connected = peer.receive("open")
            assert connected["ok"] and connected["producer_pid"] == opened["producer_pid"]
            assert connected["producer_creation"] == opened["producer_creation_filetime"]
            assert peer.request("peek")["ok"]
            producer.expected_kill = True
            producer.process.stdin.write("crash\n")
            producer.process.stdin.flush()
            assert producer.process.wait(timeout=5) == 73
            rejected = peer.request("accept")
            assert not rejected["ok"] and rejected["error"] == 10 and rejected["head"] == 0, rejected
            done = retire(peer)
    return dict(actual_producer_pid=opened["producer_pid"], python_launcher_pid=producer.process.pid,
                expected_process_termination=True, abrupt_exit_code=73,
                rejection=rejected, retirement=done, retry_prohibited=True)


def run_redirector_cleanup(directory):
    scope = new_scope()
    command = [sys.executable, Path(__file__), "--producer-child", json.dumps(scope.request())]
    producer = ProcessPeer(command, directory, "producer")
    try:
        opened = producer.receive("producer_open")
        producer.retain_actual_producer(opened["producer_pid"], opened["producer_creation_filetime"])
        assert producer.request("hang")["producer_pid"] == opened["producer_pid"]
    except BaseException:
        producer.close()
        raise
    # The harness must report failed cleanup, not convert forced termination into
    # normal success. This separate case intentionally exercises that failure path.
    try:
        producer.close(timeout=.25)
    except AssertionError as error:
        assert str(error) == "Private child did not exit on EOF", error
    else:
        raise AssertionError("A deliberately hung producer unexpectedly exited on EOF")
    assert producer.forced and producer.process.poll() is not None
    assert producer.actual_process is None and not producer.thread.is_alive()
    return dict(actual_producer_pid=opened["producer_pid"], python_launcher_pid=producer.process.pid,
                deliberately_hung_child=True, forced_cleanup_reported_as_failure=True,
                actual_child_and_launcher_closed=True, stdout_thread_joined=True)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--producer-child":
        values = json.loads(sys.argv[2])
        values["epoch"], values["generation"] = int(values["epoch"]), int(values["generation"])
        producer_child(Scope(**values))
        return
    destination = Path(sys.argv[1]).resolve()
    destination.relative_to((ROOT / "artifacts").resolve())
    destination.mkdir(parents=True, exist_ok=False)
    before = {str(p.relative_to(ROOT)): digest(p.read_bytes()) for p in TRACKED}
    assert before[str(CLI.relative_to(ROOT))] == CLI_SHA, "CLI was not the frozen build"
    result = dict(started_at=datetime.now().astimezone().isoformat(), actual_windows_ipc=True,
                  actual_file_decode=True, actual_distinct_native_process=True, fixture_scope_only=True,
                  live_deployment=False, broker_verified=False, network_verified=False,
                  device_consumption_verified=False, quest_verified=False, cases=[])
    try:
        cases = [("first-full-wrap", lambda d: run_vector(d, False)),
                 ("seek-full-wrap", lambda d: run_vector(d, True)), ("wrong-scope", run_wrong_scope),
                 ("duplicate-consumer", run_duplicate),
                 ("producer-close-after-peek", lambda d: run_changed_after_peek(d, False)),
                 ("corrupt-after-peek", lambda d: run_changed_after_peek(d, True)),
                 ("held-mutex-retire-retry", run_mutex_retire),
                 ("consumer-process-loss", run_consumer_loss), ("producer-process-loss", run_producer_loss),
                 ("redirector-cleanup-fallback", run_redirector_cleanup)]
        for name, case in cases:
            directory = destination / name
            directory.mkdir()
            started = time.perf_counter_ns()
            try:
                details = case(directory)
            except BaseException:
                result["cases"].append(dict(name=name, passed=False, traceback=traceback.format_exc()))
                raise
            result["cases"].append(dict(name=name, passed=True,
                duration_ms=(time.perf_counter_ns() - started) / 1e6, **details))
            print(json.dumps(dict(case=name, passed=True)), flush=True)
        result["passed"] = True
    finally:
        after = {str(p.relative_to(ROOT)): digest(p.read_bytes()) for p in TRACKED}
        result.update(finished_at=datetime.now().astimezone().isoformat(), source_sha256=before,
                      source_unchanged=before == after, source_after_sha256=after)
        if before != after:
            result["passed"] = False
        for path in TRACKED:
            if path.suffix not in (".exe", ".nut"):
                copied = destination / "source-snapshot" / path.relative_to(ROOT)
                copied.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, copied)
        (destination / "verification.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    assert result["passed"] and result["source_unchanged"]
    print(json.dumps(dict(directory=str(destination), passed=len(result["cases"]))))


if __name__ == "__main__":
    main()
