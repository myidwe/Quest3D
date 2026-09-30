"""Mocked ADB regression for raw-byte preservation; never contacts a device."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "native/diagnostics/select_quest_codec.py"
SPEC = importlib.util.spec_from_file_location("select_quest_codec_test_module", SOURCE)
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)

APP = "files/app_state.cfg"
HOST = "files/host_state.cfg"
PAIRING = "files/addons/nightfall-stream/config.ini"
ACTIVITY = mod.PACKAGE + "/com.godot.game.GodotAppLauncher"


def digest(data):
    return hashlib.sha256(data).hexdigest()


class FakeADB:
    """One exact device/package, with shell terminal translation and raw exec IO."""

    def __init__(self, newline=b"\n", preference=0, fault=None):
        self.files = {
            APP: newline.join([b"[settings]", b"brightness_pct=0", b"contrast_pct=100",
                 b"sharpen_mode=7", f"codec_preference={preference}".encode(), b"screen_width=2.7104", b""]),
            HOST: b"[paired-host]\nfps=72\nbitrate_idx=-1\nnative_resolution=[1920, 1080]\npicture_temperature=-14\n",
            PAIRING: b"[fixture-only-pairing]\nidentity=private-fixture-owner\n",
        }
        self.before = self.files.copy()
        self.calls, self.mutations = [], []
        self.fault, self.running = fault, True
        self.installed_hash = mod.BASELINE_APK

    @staticmethod
    def terminal(data):
        return data.replace(b"\n", b"\r\n")

    def check_output(self, command, *, input=None, timeout=None):
        assert command[:3] == [str(mod.ADB), "-s", mod.SERIAL]
        assert timeout == 40
        args = tuple(command[3:])
        self.calls.append((args, input))
        if args == ("get-state",):
            return b"offline\n" if self.fault == "offline" else b"device\n"
        if args == ("shell", "pm", "path", mod.PACKAGE):
            return b"package:/data/app/fixture-reviewed/base.apk\r\n"
        if args == ("shell", "sha256sum", "/data/app/fixture-reviewed/base.apk"):
            return self.installed_hash.encode() + b"  /data/app/fixture-reviewed/base.apk\r\n"
        if len(args) == 5 and args[1:4] == ("run-as", mod.PACKAGE, "cat"):
            raw = self.files[args[4]]
            if args[0] == "shell" or (args[0] == "exec-out" and self.fault == "raw_transfer_crlf"):
                return self.terminal(raw)
            assert args[0] == "exec-out"
            return raw
        if len(args) == 5 and args[:4] == ("shell", "run-as", mod.PACKAGE, "sha256sum"):
            value = "0" * 64 if self.fault == "device_hash" else digest(self.files[args[4]])
            return value.encode() + b"  " + args[4].encode() + b"\r\n"
        if args == ("shell", "am", "force-stop", mod.PACKAGE):
            self.running = False
            self.mutations.append("stop")
            if self.fault in ("app_changes_on_stop", "host_changes_on_stop", "pairing_changes_on_stop"):
                target = {"app_changes_on_stop": APP, "host_changes_on_stop": HOST,
                          "pairing_changes_on_stop": PAIRING}[self.fault]
                self.files[target] += b"changed-during-stop\n"
            return b""
        expected_write = (f"run-as {mod.PACKAGE} sh -c 'cat > files/app_state.codec-quality.tmp "
                          "&& mv -f files/app_state.codec-quality.tmp files/app_state.cfg'")
        if args == ("exec-in", expected_write):
            assert not self.running, "Settings cannot be replaced while the app owns them"
            assert isinstance(input, bytes)
            self.mutations.append("write")
            self.files[APP] = input
            if self.fault == "written_content": self.files[APP] += b"unexpected\n"
            if self.fault == "pairing_changes_after_write": self.files[PAIRING] += b"unexpected\n"
            if self.fault == "host_changes_after_write": self.files[HOST] += b"unexpected\n"
            return b""
        if args == ("shell", "am", "start", "-W", "-n", ACTIVITY):
            self.mutations.append("launch")
            if self.fault == "launch_exit":
                raise subprocess.CalledProcessError(1, command, output=b"am launch failed")
            self.running = True
            if self.fault == "launch_status":
                return b"Starting: Intent { cmp=" + ACTIVITY.encode() + b" }\r\nError type 3\r\n"
            launched = b"com.android.systemui/.DifferentActivity" if self.fault == "launch_wrong_activity" else ACTIVITY.encode()
            if self.fault == "launch_actual_app": launched = (mod.PACKAGE + "/com.godot.game.GodotApp").encode()
            # Intent includes the requested target even when actual Activity differs.
            return (b"Starting: Intent { cmp=" + ACTIVITY.encode() + b" }\r\nStatus: ok\r\nActivity: "
                    + launched + b"\r\nTotalTime: 25\r\nComplete\r\n")
        if args == ("shell", "pidof", mod.PACKAGE):
            if self.fault == "no_pid": return b""
            if self.fault == "multiple_pids": return b"15324 15325\r\n"
            return b"15324\r\n" if self.running else b""
        raise AssertionError(f"Unreviewed ADB operation: {args!r}")


def configure(tmp_path, monkeypatch, fake, *, old="h264", new="hevc"):
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod.subprocess, "check_output", fake.check_output)
    output = tmp_path / "artifacts" / "journal"
    monkeypatch.setattr(sys, "argv", [str(SOURCE), "--codec", new, "--expected-current", old, "--output", str(output)])
    return output


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
@pytest.mark.parametrize("old,new,index", [("h264", "hevc", 0), ("hevc", "h264", 1)])
def test_only_digit_changes_and_all_raw_state_preserved(tmp_path, monkeypatch, newline, old, new, index):
    fake = FakeADB(newline, index)
    output = configure(tmp_path, monkeypatch, fake, old=old, new=new)
    mod.main()
    expected = fake.before[APP].replace(f"codec_preference={index}".encode(), f"codec_preference={1-index}".encode())
    assert fake.files[APP] == expected
    assert sum(a != b for a, b in zip(fake.before[APP], expected)) == 1
    assert len(fake.before[APP]) == len(expected)
    assert fake.files[HOST] == fake.before[HOST] and fake.files[PAIRING] == fake.before[PAIRING]
    assert (output / "app-before.cfg").read_bytes() == fake.before[APP]
    assert (output / "host-before.cfg").read_bytes() == fake.before[HOST]
    assert (output / "pairing-before.ini").read_bytes() == fake.before[PAIRING]
    assert (output / "app-candidate.cfg").read_bytes() == expected
    assert fake.mutations == ["stop", "write", "launch"]
    reads = [args for args, _ in fake.calls if len(args) > 3 and args[3] == "cat"]
    assert reads and all(args[0] == "exec-out" for args in reads)
    record = json.loads((output / "result.json").read_text())
    assert record["status"] == "preference_applied_app_launched"
    assert record["before_sha256"] == digest(fake.before[APP]) and record["after_sha256"] == digest(expected)
    assert record["pid"] == 15324 and record["launch_status_ok"]
    assert record["pairing_and_host_bytes_preserved_before_launch"]
    assert not record["decoder_verified"] and not record["wearer_verified"]


@pytest.mark.parametrize("fault", ["raw_transfer_crlf", "device_hash", "offline"])
def test_terminal_translation_or_untrusted_raw_hash_refused_before_stop(tmp_path, monkeypatch, fault):
    fake = FakeADB(fault=fault)
    output = configure(tmp_path, monkeypatch, fake)
    assert FakeADB.terminal(fake.files[APP]) != fake.files[APP]
    with pytest.raises(RuntimeError): mod.main()
    assert not fake.mutations and fake.files == fake.before and not output.exists()


@pytest.mark.parametrize("fault", ["app_changes_on_stop", "host_changes_on_stop", "pairing_changes_on_stop"])
def test_state_change_at_stop_refuses_write_and_preserves_journal(tmp_path, monkeypatch, fault):
    fake = FakeADB(fault=fault)
    output = configure(tmp_path, monkeypatch, fake)
    with pytest.raises(RuntimeError, match="shutdown changed settings"): mod.main()
    assert fake.mutations == ["stop"] and not fake.running
    assert (output / "app-before.cfg").read_bytes() == fake.before[APP]
    assert json.loads((output / "result.json").read_text())["status"] == "failed"


@pytest.mark.parametrize("fault", ["written_content", "pairing_changes_after_write", "host_changes_after_write"])
def test_post_write_mismatch_stops_before_launch(tmp_path, monkeypatch, fault):
    fake = FakeADB(fault=fault)
    output = configure(tmp_path, monkeypatch, fake)
    with pytest.raises(RuntimeError): mod.main()
    assert fake.mutations == ["stop", "write"] and not fake.running
    assert (output / "app-before.cfg").read_bytes() == fake.before[APP]
    record = json.loads((output / "result.json").read_text())
    assert record["status"] == "failed" and "error" in record
    # Failure retains the real modified state and candidate; no fake rollback receipt.
    assert (output / "app-candidate.cfg").exists()


@pytest.mark.parametrize("fault", ["launch_exit", "launch_status", "no_pid", "multiple_pids", "launch_wrong_activity"])
def test_launch_failure_never_claims_launched(tmp_path, monkeypatch, fault):
    fake = FakeADB(fault=fault)
    output = configure(tmp_path, monkeypatch, fake)
    with pytest.raises((RuntimeError, subprocess.CalledProcessError)): mod.main()
    record = json.loads((output / "result.json").read_text())
    assert record["status"] == "failed" and "error" in record
    assert not record["decoder_verified"] and not record["wearer_verified"]
    assert fake.files[APP] == fake.before[APP].replace(b"codec_preference=0", b"codec_preference=1")


def test_wrong_apk_refused_without_state_read_or_mutation(tmp_path, monkeypatch):
    fake = FakeADB()
    fake.installed_hash = "0" * 64
    configure(tmp_path, monkeypatch, fake)
    with pytest.raises(RuntimeError, match="Installed APK differs"): mod.main()
    assert not fake.mutations and not any(args[0] == "exec-out" for args, _ in fake.calls)


def test_reviewed_launcher_alias_resolves_to_actual_godot_app(tmp_path, monkeypatch):
    fake = FakeADB(fault="launch_actual_app")
    output = configure(tmp_path, monkeypatch, fake)
    mod.main()
    record = json.loads((output / "result.json").read_text())
    assert record["status"] == "preference_applied_app_launched" and record["pid"] == 15324
    assert b"Activity: " + (mod.PACKAGE + "/com.godot.game.GodotApp").encode() in (output / "launch.log").read_bytes()


def test_changed_preference_refused_before_stop(tmp_path, monkeypatch):
    fake = FakeADB(preference=1)
    configure(tmp_path, monkeypatch, fake)
    with pytest.raises(RuntimeError, match="Preference changed"): mod.main()
    assert not fake.mutations and fake.files == fake.before


def test_existing_journal_refused_before_any_adb(tmp_path, monkeypatch):
    fake = FakeADB()
    output = configure(tmp_path, monkeypatch, fake)
    output.mkdir(parents=True)
    (output / "evidence.json").write_text("prior")
    with pytest.raises(FileExistsError): mod.main()
    assert not fake.calls and (output / "evidence.json").read_text() == "prior"
