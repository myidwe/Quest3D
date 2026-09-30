"""Bind a staged host's control directory to the actual video bridge epoch."""
import argparse
import json
import math
from pathlib import Path
import re
import time

from quest3d.session_control import read_json


def integer(value, name, minimum=0, maximum=(1 << 64) - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"Invalid {name}")
    return value


def validate(directory, config, *, probe_path=None, expected=None, now_ns=None, enable_input=False):
    directory = Path(directory).resolve(strict=True)
    config = Path(config).resolve(strict=True)
    if config.stat().st_size > 65536:
        raise ValueError("Host config exceeds the bounded preflight size")
    settings = config.read_text(encoding="utf-8-sig")
    controls = re.findall(r"^\s*quest3d_control_dir\s*=\s*(.*?)\s*$", settings, re.M | re.I)
    if len(controls) != 1 or Path(controls[0]).resolve(strict=True) != directory:
        raise ValueError("Host config and source control directory differ")
    protocols = re.findall(r"^\s*quest3d_protocol\s*=\s*(.*?)\s*$", settings, re.M | re.I)
    if len(protocols) > 1 or (protocols and protocols[0] not in ("2", "3")):
        raise ValueError("Invalid or duplicate configured bridge protocol")
    protocol = int(protocols[0]) if protocols else 2
    input_settings = re.findall(r"^\s*quest3d_input\s*=\s*(.*?)\s*$", settings, re.M | re.I)
    if len(input_settings) > 1 or (input_settings and input_settings[0] not in ("enabled", "disabled")):
        raise ValueError("Invalid or duplicate configured input gate")
    configured_input = bool(input_settings and input_settings[0] == "enabled")
    if type(enable_input) is not bool or configured_input != enable_input or (enable_input and protocol != 3):
        raise ValueError("Input requires matching explicit launch/config opt-in and bridge v3")
    status = read_json(directory / "status.json", max_bytes=65536)
    # Legacy desktop status omits input_enabled; the actual bridge flags are
    # still mandatory at launch. An explicit enabled/invalid status is refused.
    if (type(status) is not dict or status.get("running") is not True
            or status.get("input_enabled", False) is not enable_input):
        raise ValueError("Expected a live source with input " + ("enabled" if enable_input else "disabled"))
    if enable_input and (status.get("input_opt_in") is not True
            or status.get("requested_mode") != "2d" or status.get("effective_mode") != "2d"
            or status.get("error") is not None):
        raise ValueError("Input launch requires an opted-in original-2D source without an error")
    if integer(status.get("bridge_protocol", 2), "source bridge protocol", 2, 3) != protocol:
        raise ValueError("Host config and source bridge protocol differ")
    session = status.get("session_id")
    if type(session) is not str or re.fullmatch(r"[0-9a-f]{32}", session) is None:
        raise ValueError("Invalid source session identity")
    epoch = integer(status.get("stream_epoch"), "source stream epoch", 1)
    width = integer(status.get("eye_width"), "eye width", 1, 2048) * 2
    height = integer(status.get("eye_height"), "eye height", 1, 2160)
    timestamp = integer(status.get("updated_monotonic_ns"), "source timestamp")
    now_ns = time.perf_counter_ns() if now_ns is None else now_ns
    if not 0 <= now_ns - timestamp <= 2_000_000_000:
        raise ValueError("Source status is stale or in the future")
    result = {"version": 1, "control_directory": str(directory), "session_id": session,
              "stream_epoch": str(epoch), "width": width, "height": height, "bridge_protocol": protocol,
              "input_opt_in": enable_input}
    if expected is not None and any(expected.get(key) != value for key, value in result.items()):
        raise ValueError("Source changed after staging; prepare a new host runtime")
    count = 0
    if probe_path is not None:
        probe_path = Path(probe_path)
        probe_stat = probe_path.stat()
        if probe_stat.st_size > 65536:
            raise ValueError(f"Video probe is oversized ({probe_stat.st_size} > 65536 bytes)")
        probe_age_ns = time.time_ns() - probe_stat.st_mtime_ns
        if probe_age_ns < 0:
            raise ValueError(f"Video probe timestamp is in the future (age_ms={probe_age_ns / 1e6:.3f})")
        if probe_age_ns > 2_000_000_000:
            raise ValueError(f"Video probe is stale (age_ms={probe_age_ns / 1e6:.3f} > 2000)")
        lines = probe_path.read_text(encoding="utf-8-sig").splitlines()
        if not 2 <= len(lines) <= 128:
            raise ValueError("Expected bounded actual video probe frames")
        previous_id = 0
        for line in lines:
            frame = json.loads(line)
            if type(frame) is not dict:
                raise ValueError("Invalid video probe frame")
            if integer(frame.get("bridge_protocol", 2), "probe bridge protocol", 2, 3) != protocol:
                raise ValueError("Actual video probe protocol differs from the selected source")
            if (integer(frame.get("stream_epoch"), "video epoch", 1) != epoch
                    or integer(frame.get("width"), "video width", 1) != width
                    or integer(frame.get("height"), "video height", 1) != height):
                raise ValueError("Actual video bridge and control source differ")
            flags = integer(frame.get("flags"), "video flags", 0, 15)
            if not enable_input and (not flags & 1 or flags & 8):
                raise ValueError("Expected Full-SBS video with input disabled")
            if enable_input and (flags != 15 or frame.get("source_kind") != "monitor"):
                raise ValueError("Input requires actual v3 original-2D monitor frames with permission")
            age = frame.get("source_age_ms")
            # A paused file/photo can legitimately repeat old source pixels.
            # Require advancing publications, not a young original media PTS.
            if type(age) not in (int, float) or not math.isfinite(age) or age < 0:
                raise ValueError("Video frame age is invalid")
            if enable_input and age > 500:
                raise ValueError("Input source capture is stale")
            frame_id = integer(frame.get("frame_id"), "publication frame ID", 1)
            if frame_id <= previous_id:
                raise ValueError("Actual video publications do not advance")
            previous_id = frame_id
            count += 1
    return {**result, "actual_frames_checked": count}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("config", type=Path)
    parser.add_argument("--probe", type=Path)
    parser.add_argument("--expected-manifest", type=Path)
    parser.add_argument("--enable-input", action="store_true")
    args = parser.parse_args()
    try:
        expected = None
        if args.expected_manifest:
            expected = read_json(args.expected_manifest, max_bytes=65536).get("source_session")
            if type(expected) is not dict:
                raise ValueError("Staged runtime has no source identity; prepare it again")
        print(json.dumps(validate(args.directory, args.config, probe_path=args.probe,
                                  expected=expected, enable_input=args.enable_input)))
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"valid": False, "reason": f"{type(exc).__name__}: {exc}"}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
