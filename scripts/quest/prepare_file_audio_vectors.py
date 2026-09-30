"""Generate actual A/V decode vectors; no HTTP/device/Quest playback is claimed."""
from datetime import datetime
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
from test_media_audio import make_av
from quest3d.file_audio_protocol import Scope, Block, encode, decode, eof
from quest3d.media_audio import MediaAudioReader


def main():
    destination = Path(sys.argv[1]).resolve()
    destination.relative_to((ROOT / "artifacts").resolve())
    destination.mkdir(parents=True, exist_ok=False)
    source = destination / "known-av.nut"
    expected = make_av(source, duration=Fraction(5003, 48000), audio_start=Fraction(0), video_start=Fraction(0))
    sha = lambda data: hashlib.sha256(data).hexdigest()
    result = dict(generated_at=datetime.now().astimezone().isoformat(), actual_file_decode=True,
                  network_verified=False, device_verified=False, quest_verified=False, cases=[])
    for name, seek_ns, epoch, generation in (("first", None, 7, 9), ("seek", 20_000_000, 8, 10)):
        scope = Scope("11" * 16, "22" * 16, "33" * 16, epoch, generation)
        blocks, rows = [], []
        with MediaAudioReader(source) as reader:
            if seek_ns is not None:
                reader.seek(seek_ns, epoch)
            while True:
                try:
                    pcm = reader.next()
                except StopIteration:
                    break
                # This standalone wire test binds decoded data explicitly to
                # its fixture scope. Product uses from_scheduled's real token.
                block = Block(scope, len(blocks), len(blocks), pcm.pts_ns, len(pcm.samples), 0,
                              pcm.samples, discontinuity=pcm.discontinuity)
                wire = encode(block)
                decoded = decode(wire, scope)
                np.testing.assert_array_equal(decoded.samples, pcm.samples)
                blocks.append(wire)
                rows.append(dict(sequence=block.sequence, block_id=block.block_id, pts_ns=block.pts_ns,
                    total_samples=block.total_samples, offset_samples=block.offset_samples, count=block.count,
                    discontinuity=block.discontinuity, pcm_sha256=sha(block.samples.tobytes())))
                final_pts = pcm.end_pts_ns
        expected_pcm = expected[0 if seek_ns is None else 960:]
        decoded_pcm = np.concatenate([decode(wire, scope).samples for wire in blocks])
        np.testing.assert_array_equal(decoded_pcm, expected_pcm)
        assert rows[-1]["count"] == 203
        blocks.append(encode(eof(scope, len(blocks), len(blocks), final_pts)))
        (destination / (name + ".wire")).write_bytes(b"".join(blocks))
        (destination / (name + ".pcm")).write_bytes(decoded_pcm.astype("<f4", copy=False).tobytes())
        result["cases"].append(dict(name=name, scope=scope.request(), seek_ns=seek_ns,
            frames=len(decoded_pcm), blocks=len(blocks), final_pts_ns=final_pts,
            payload_sha256=sha(decoded_pcm.tobytes()), rows=rows))
    result["files"] = {p.name: sha(p.read_bytes()) for p in destination.iterdir() if p.is_file()}
    result["source_sha256"] = {str(p.relative_to(ROOT)): sha(p.read_bytes()) for p in (
        Path(__file__), ROOT / "src/quest3d/file_audio_protocol.py", ROOT / "src/quest3d/media_audio.py",
        ROOT / "tests/test_media_audio.py")}
    (destination / "manifest.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(dict(directory=str(destination), cases=[{k:r[k] for k in
        ("name", "frames", "blocks", "final_pts_ns")} for r in result["cases"]])))


if __name__ == "__main__":
    main()
