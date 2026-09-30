"""Read-only file PCM launch preflight; never register a native consumer."""
import argparse
import json
from pathlib import Path
import time

from quest3d.audio_bridge import _identifier, _integer, inspect_channel
from quest3d.session_control import read_json


FIELDS = {"version", "session_id", "file_session_id", "channel", "producer_pid", "producer_creation_filetime"}


def validate(directory):
    directory = Path(directory).resolve(strict=True)
    manifest = read_json(directory / "audio-channel.json", max_bytes=4096)
    status = read_json(directory / "status.json", max_bytes=65536)
    if type(manifest) is not dict or set(manifest) != FIELDS or type(manifest["version"]) is not int or manifest["version"] != 1:
        raise ValueError("Invalid file PCM manifest schema")
    for key in ("session_id", "file_session_id", "channel"): _identifier(manifest[key])
    for key, maximum in (("producer_pid", 0xffffffff), ("producer_creation_filetime", (1 << 63) - 1)):
        if not _integer(manifest[key], key, maximum): raise ValueError("Invalid PCM producer identity")
    if (type(status) is not dict or status.get("session_id") != manifest["session_id"]
            or status.get("running") is not True or status.get("input_enabled") is not False
            or status.get("file_clock") != "common-av-candidate" or status.get("audio_requested") is not True
            or type(status.get("audio_integrated")) is not bool):
        raise ValueError("Status does not describe the requested live file PCM session")
    updated = _integer(status.get("updated_monotonic_ns"), "status timestamp")
    video_epoch = _integer(status.get("stream_epoch"), "video stream epoch", (1 << 64) - 1)
    if not video_epoch: raise ValueError("Missing video stream epoch")
    if not 0 <= time.perf_counter_ns() - updated <= 2_000_000_000:
        raise ValueError("File PCM status is stale or from a future clock")
    if type(status.get("media")) is not dict or status["media"].get("session_id") != manifest["file_session_id"]:
        raise ValueError("File playback session differs from the channel manifest")
    audio = status.get("audio")
    if (type(audio) is not dict or audio.get("file_session") != manifest["file_session_id"]
            or any(audio.get(key) != manifest[key] for key in ("channel", "producer_pid", "producer_creation_filetime"))
            or audio.get("fault") is not None or audio.get("error") is not None):
        raise ValueError("PCM status identity or fault state differs from the manifest")
    descriptor = inspect_channel(manifest["channel"])
    if any(descriptor[key] != manifest[key] for key in ("file_session_id", "channel", "producer_pid", "producer_creation_filetime")):
        raise ValueError("Actual PCM mapping identity differs from the manifest")
    # A consumer cannot be replaced in-place: a new native lifetime needs a new
    # FileAV/channel session and an explicit common transport-reset transition.
    if descriptor["consumer_pid"] or descriptor["consumer_ready"] or status["audio_integrated"]:
        raise RuntimeError("PCM channel already has a consumer; prepare a fresh file session")
    return {**manifest, "control_directory": str(directory), "video_stream_epoch": video_epoch,
            "validated_at_monotonic_ns": time.perf_counter_ns()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(validate(args.directory)))
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        # Do not echo JSON contents, media file paths, or credentials.
        print(json.dumps({"valid": False, "reason": type(exc).__name__ + ": " + str(exc)}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
