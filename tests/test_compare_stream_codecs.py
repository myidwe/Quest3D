"""CPU-only input/metric/ledger tests; NVENC is never opened by this suite."""
from __future__ import annotations

from fractions import Fraction
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import av
import numpy as np
from PIL import Image
import pytest

PATH = Path(__file__).resolve().parents[1] / "native/diagnostics/compare_stream_codecs.py"
SPEC = importlib.util.spec_from_file_location("compare_stream_codecs_test_module", PATH)
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


def fixture_sequence(tmp_path, count=3, bgra=False):
    rows = []
    yy, xx = np.indices((32, 64))
    for i in range(count):
        rgb = np.stack(((xx * 4 + i * 13) % 256, yy * 8, ((xx >= 32) * 160 + i * 15) % 256), axis=2).astype(np.uint8)
        if bgra:
            image = np.empty((32, 64, 4), dtype=np.uint8)
            image[..., :3] = rgb[..., ::-1]
            image[..., 3] = 255
            path = tmp_path / f"source-{i}.npy"
            np.save(path, image, allow_pickle=False)
        else:
            path = tmp_path / f"source-{i}.png"
            Image.fromarray(rgb).save(path)
        rows.append({"path": path.name, "sha256": mod.sha256(path), "frame_id": 100 + i,
                     "captured_ns": 1000000 + i * 33333333, "stream_epoch": "epoch-exact"})
    manifest = {"schema": 1, "pixel_format": "bgra" if bgra else "rgb24", "frames": rows,
                "source_nominal_fps": 30, "actual_stream_requested_fps": 72,
                "rois": [{"name": "test", "kind": "edge", "eye": "both", "rect": [2, 2, 24, 24]}]}
    path = tmp_path / "sequence.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path, manifest


def rewrite(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.mark.parametrize("bgra", [False, True])
def test_prepare_exact_identity_and_cpu_only(tmp_path, monkeypatch, bgra):
    path, _ = fixture_sequence(tmp_path, bgra=bgra)
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "encode_case", lambda *a, **k: pytest.fail("prepare-only opened encoder"))
    output = tmp_path / "artifacts" / "prepared"
    assert mod.main(["--sequence", str(path), "--output", str(output), "--fps", "30", "--prepare-only"]) == 0
    result = json.loads((output / "result.json").read_text())
    assert result["status"] == "prepared_cpu_only" and not result["gpu_encode_requested"]
    assert result["sequence"]["frames"][2]["frame_id"] == 102
    assert result["sequence"]["frames"][2]["captured_ns"] == 67666666
    assert result["sequence"]["frames"][2]["stream_epoch"] == "epoch-exact"
    assert result["sequence"]["actual_stream_requested_fps"] == 72
    matrix = np.load(output / "common-yuv420p.npy", allow_pickle=False)
    expected = mod.rgb_to_yuv(mod.read_rgb(result["sequence"]["frames"][0]))
    np.testing.assert_array_equal(matrix[0], expected)
    assert mod.sha256(output / "common-yuv420p.npy") == result["preparation"]["sha256"]
    assert set(result["preparation"]["rgb_to_420_roundtrip"]) == {"full", "left", "right", "edge:test:left", "edge:test:right"}


def test_bgra_order_is_rgb_and_source_bytes_preserved(tmp_path):
    path, manifest = fixture_sequence(tmp_path, bgra=True)
    sequence = mod.load_sequence(path)
    original = np.load(tmp_path / manifest["frames"][0]["path"])
    rgb = mod.read_rgb(sequence["frames"][0])
    np.testing.assert_array_equal(rgb, original[..., [2, 1, 0]])
    assert mod.sha256(tmp_path / manifest["frames"][0]["path"]) == manifest["frames"][0]["sha256"]


@pytest.mark.parametrize("mutation", ["hash", "shape", "dtype", "alpha", "roi", "conflicting_id", "captured_ns", "pixel_format"])
def test_invalid_input_rejected(tmp_path, mutation):
    path, manifest = fixture_sequence(tmp_path, bgra=True)
    row = manifest["frames"][1]
    if mutation == "hash": row["sha256"] = "0" * 64
    elif mutation in ("shape", "dtype", "alpha"):
        data = np.load(tmp_path / row["path"])
        if mutation == "shape": data = data[:, :-4]
        elif mutation == "dtype": data = data.astype(np.float32)
        else: data[..., 3] = 0
        np.save(tmp_path / row["path"], data, allow_pickle=False)
        row["sha256"] = mod.sha256(tmp_path / row["path"])
    elif mutation == "roi": manifest["rois"][0]["rect"] = [30, 0, 24, 24]
    elif mutation == "conflicting_id": row["frame_id"] = manifest["frames"][0]["frame_id"]
    elif mutation == "captured_ns": row["captured_ns"] = True
    elif mutation == "pixel_format": manifest["pixel_format"] = "rgba"
    rewrite(path, manifest)
    with pytest.raises(ValueError): mod.load_sequence(path)


def test_explicit_repeat_and_new_epoch_are_distinct(tmp_path):
    path, manifest = fixture_sequence(tmp_path)
    manifest["frames"].insert(1, dict(manifest["frames"][0]))
    manifest["frames"][2]["frame_id"] = manifest["frames"][0]["frame_id"]
    manifest["frames"][2]["stream_epoch"] = "new-epoch"
    rewrite(path, manifest)
    sequence = mod.load_sequence(path)
    assert sequence["unique_source_ids"] == 3 and len(sequence["frames"]) == 4
    assert [r["slot"] for r in sequence["frames"]] == [0, 1, 2, 3]


def test_changed_after_validation_rejected(tmp_path):
    path, manifest = fixture_sequence(tmp_path)
    sequence = mod.load_sequence(path)
    (tmp_path / manifest["frames"][0]["path"]).write_bytes(b"changed")
    output = tmp_path / "prepared"
    output.mkdir()
    with pytest.raises(ValueError, match="Input changed"): mod.prepare(sequence, output)


def test_existing_output_rejected_before_input_read_or_encode(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "load_sequence", lambda *a: pytest.fail("read before output guard"))
    output = tmp_path / "artifacts" / "existing"
    output.mkdir(parents=True)
    marker = output / "untouched.json"
    marker.write_text("preserve")
    with pytest.raises(ValueError, match="already exists"):
        mod.main(["--sequence", "absent.json", "--output", str(output), "--fps", "30"])
    assert marker.read_text() == "preserve"


def test_nvenc_options_share_only_selected_variables():
    h264 = mod.encoder_options("h264", "p1", 53000, Fraction(72))
    hevc = mod.encoder_options("hevc", "p3", 53000, Fraction(72))
    assert {k: v for k, v in h264.items() if k not in ("profile", "preset")} == {
        k: v for k, v in hevc.items() if k not in ("profile", "preset")}
    assert h264["bufsize"] == str(53000000 // 72)
    assert h264["rc"] == "cbr" and h264["multipass"] == "qres"
    assert h264["ldkfs"] == "1" and h264["rc-lookahead"] == "0"
    assert h264["delay"] == "0" and h264["zerolatency"] == "1"


def test_metrics_equal_then_changed_right_eye(tmp_path):
    path, _ = fixture_sequence(tmp_path)
    seq = mod.load_sequence(path)
    rgb = mod.read_rgb(seq["frames"][0])
    same = mod.quality(rgb, rgb.copy(), mod.regions(seq))
    assert same["full"]["rgb_mse"] == 0 and same["full"]["rgb_psnr_db"] is None
    assert same["full"]["luma_ssim"] == 1 and same["full"]["gradient_residual_mae"] == 0
    actual = rgb.copy()
    actual[:, 32:, :] //= 2
    changed = mod.quality(rgb, actual, mod.regions(seq))
    assert changed["left"]["rgb_mse"] == 0 and changed["left"]["luma_ssim"] == 1
    assert changed["right"]["rgb_mse"] > 0 and changed["right"]["luma_ssim"] < 0.9
    assert changed["edge:test:right"]["gradient_residual_mae"] > 0


def test_real_cpu_encoder_saved_packet_pts_decode(tmp_path, monkeypatch):
    """Exercise actual PyAV bytes/PTS/decoder with libx264, not simulated NVENC."""
    path, _ = fixture_sequence(tmp_path, count=4)
    seq = mod.load_sequence(path)
    matrix = np.stack([mod.rgb_to_yuv(mod.read_rgb(row)) for row in seq["frames"]])
    created = []
    def create(name, mode):
        assert name in ("h264_nvenc", "h264")
        selected = "libx264" if mode == "w" else "h264"
        created.append(selected)
        return av.CodecContext.create(selected, mode)
    monkeypatch.setattr(mod, "av", SimpleNamespace(CodecContext=SimpleNamespace(create=create),
        VideoFrame=av.VideoFrame, Packet=av.Packet, logging=av.logging))
    monkeypatch.setattr(mod, "encoder_options", lambda *a: {"preset": "ultrafast", "tune": "zerolatency", "crf": "18"})
    directory = tmp_path / "cpu-case"
    directory.mkdir()
    encoded = mod.encode_case(matrix, seq, directory, "h264", "p1", 53000, Fraction(30))
    decoded = mod.decode_case(seq, matrix, directory, encoded)
    assert created == ["libx264", "h264"]
    assert [f["frame_id"] for f in decoded["frames"]] == [100, 101, 102, 103]
    assert [f["slot"] for f in decoded["frames"]] == [0, 1, 2, 3]
    assert decoded["decoder"]["visible_size"] == [64, 32]
    assert decoded["decoder"]["crop_offsets"] is None
    assert decoded["decoder"]["pix_fmt"] == "yuv420p"
    assert not decoded["decoder"]["has_b_frames"]
    assert encoded["actual_elementary_mbps"] > 0 and encoded["bitstream_bytes"] > 0
    assert decoded["codec_only"]["full"]["rgb_mse"] < decoded["source_rgb"]["full"]["rgb_mse"]
    encoded["packets"][0]["pts"] = 999
    with pytest.raises(ValueError, match="PTS"):
        mod.decode_case(seq, matrix, directory, encoded)
