"""Compare immutable SBS pixels through PyAV NVENC, never the running Sunshine host.

The manifest orders the *encoder input slots*. Repeated source frame IDs/pixels
are allowed only with identical hashes; this makes a separately constructed
30-to-72 Hz hold schedule explicit instead of pretending every slot is new AI.
No capture, AI, network, player, device or host configuration is accessed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import io
import json
from pathlib import Path
import platform
import sys
import time

import av
import cv2
import numpy as np
from PIL import Image
from av.video.reformatter import VideoReformatter

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = 1
LIMITATIONS = [
    "Offline PyAV/FFmpeg NVENC experiment, not production Sunshine or Quest evidence.",
    "Common CPU libswscale RGB24 to Rec709 limited YUV420P; Sunshine uses D3D conversion/NV12.",
    "Requested ULL/CBR/quarter-res two-pass/no-B/no-lookahead/single-frame VBV approximate Sunshine settings.",
    "FFmpeg uses a very long finite GOP, no RFI feedback, no RTP/FEC/encryption/network or decoder backpressure.",
    "Encoder call/packet-return times include this API's transfer/wait costs; not glass-to-glass latency or GPU kernel time.",
    "Unpaced input slots use a declared nominal FPS; source capture timestamps do not schedule this offline encoder.",
    "PyAV does not expose frame crop offsets here; visible/coded sizes are observations, crop offsets remain unknown.",
    "PSNR/SSIM and edge residuals measure fidelity to the same SBS, not correctness of stereo depth or optical comfort.",
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value) -> None:
    """All outputs are new; a rerun must choose another directory."""
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def _integer(value, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def read_rgb(row: dict) -> np.ndarray:
    payload = Path(row["path"]).read_bytes()
    if hashlib.sha256(payload).hexdigest() != row["sha256"]:
        raise ValueError(f"Input changed: {row['path']}")
    if row.get("pixel_format") == "bgra":
        source = np.load(io.BytesIO(payload), allow_pickle=False)
        if not isinstance(source, np.ndarray) or source.dtype != np.uint8 or source.ndim != 3 or source.shape[2] != 4:
            raise ValueError("BGRA input must be one uint8 HxWx4 NPY array")
        if not np.all(source[..., 3] == 255):
            raise ValueError("BGRA input must be opaque (alpha255); no implicit compositing")
        return np.ascontiguousarray(source[..., 2::-1])
    if row.get("pixel_format", "rgb24") != "rgb24":
        raise ValueError("pixel_format must be rgb24 (PNG) or bgra (NPY)")
    with Image.open(io.BytesIO(payload)) as image:
        if image.format != "PNG" or image.mode != "RGB":
            raise ValueError("Input must be an opaque RGB PNG; no implicit palette/alpha/color conversion")
        if image.info.get("icc_profile"):
            raise ValueError("ICC-tagged PNG requires an explicit upstream conversion to sRGB/Rec709 primaries")
        return np.array(image)


def load_sequence(path: Path) -> dict:
    path = path.resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema") != SCHEMA or not isinstance(manifest.get("frames"), list):
        raise ValueError("Sequence requires schema:1 and frames:[...]")
    if not 1 <= len(manifest["frames"]) <= 10000:
        raise ValueError("Expected 1..10000 explicitly ordered encoder input slots")
    rows, identities, shape = [], {}, None
    for index, original in enumerate(manifest["frames"]):
        row = dict(original)
        row.setdefault("pixel_format", manifest.get("pixel_format", "rgb24"))
        header = row.get("header", {})
        if "stream_epoch" not in row and "stream_epoch" in header:
            row["stream_epoch"] = header["stream_epoch"]
        for outer, inner in (("frame_id", "frame_id"), ("captured_ns", "capture_ns"), ("stream_epoch", "stream_epoch")):
            if outer in row and inner in header and row[outer] != header[inner]:
                raise ValueError(f"Frame ledger disagrees with captured header: {outer}")
        identity = row.get("frame_id")
        if isinstance(identity, bool) or not isinstance(identity, (str, int)) or not str(identity):
            raise ValueError("Every slot requires a nonempty source frame_id")
        if isinstance(identity, int) and identity < 0:
            raise ValueError("frame_id cannot be negative")
        expected = row.get("sha256", "")
        if not isinstance(expected, str) or len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
            raise ValueError("Every frame requires its lowercase source-file sha256")
        source_path = Path(row["path"])
        row["path"] = str((path.parent / source_path).resolve() if not source_path.is_absolute() else source_path.resolve())
        epoch = row.get("stream_epoch")
        if epoch is not None and (isinstance(epoch, bool) or not isinstance(epoch, (int, str)) or not str(epoch)):
            raise ValueError("stream_epoch must be a nonempty integer/string identity")
        key = (type(epoch).__name__, str(epoch), type(identity).__name__, str(identity))
        if key in identities and identities[key] != expected:
            raise ValueError("The same stream_epoch/frame_id cannot identify different pixels")
        identities[key] = expected
        if "captured_ns" in row:
            _integer(row["captured_ns"], "captured_ns")
        rgb = read_rgb(row)
        current_shape = tuple(rgb.shape)
        if shape is None:
            shape = current_shape
        if current_shape != shape:
            raise ValueError("Geometry changed inside the sequence; every comparison must use identical geometry")
        if shape[1] % 4 or shape[0] % 2 or shape[1] < 24 or shape[0] < 12:
            raise ValueError("Full SBS width must be divisible by 4 (>=24), height by 2 (>=12)")
        row["slot"] = index
        rows.append(row)
    height, width, _ = shape
    rois = manifest.get("rois", [])
    if not isinstance(rois, list):
        raise ValueError("rois must be a list")
    names = set()
    for roi in rois:
        name = roi.get("name")
        if not isinstance(name, str) or not name or name in names or name in ("full", "left", "right"):
            raise ValueError("ROI names must be nonempty, unique, and distinct from full/left/right")
        names.add(name)
        if roi.get("kind") not in ("edge", "text") or roi.get("eye") not in ("left", "right", "both"):
            raise ValueError("ROI kind must be edge/text and eye must be left/right/both")
        rect = roi.get("rect")
        if not isinstance(rect, list) or len(rect) != 4:
            raise ValueError("ROI rect must be [x,y,width,height] in eye-local pixels")
        x, y, w, h = [_integer(v, "ROI coordinate") for v in rect]
        if min(w, h) < 11 or x + w > width // 2 or y + h > height:
            raise ValueError("ROI must fit one eye and be at least 11x11 for local SSIM")
    timestamps = [row["captured_ns"] for row in rows if "captured_ns" in row]
    return {"schema": SCHEMA, "manifest": str(path), "manifest_sha256": sha256(path),
            "description": manifest.get("description"), "source_provenance": manifest.get("source_provenance"),
            "source_nominal_fps": manifest.get("source_nominal_fps"),
            "actual_stream_requested_fps": manifest.get("actual_stream_requested_fps"),
            "capture_timestamp_span_seconds": (max(timestamps) - min(timestamps)) / 1e9 if timestamps else None,
            "frames": rows, "unique_source_ids": len(identities), "packed_size": [width, height],
            "eye_size": [width // 2, height], "rois": rois}


def regions(sequence: dict) -> dict:
    width, height = sequence["packed_size"]
    eye = width // 2
    result = {"full": (0, 0, width, height), "left": (0, 0, eye, height), "right": (eye, 0, eye, height)}
    for roi in sequence["rois"]:
        for side in (("left", "right") if roi["eye"] == "both" else (roi["eye"],)):
            x, y, w, h = roi["rect"]
            result[f"{roi['kind']}:{roi['name']}:{side}"] = (x + (eye if side == "right" else 0), y, w, h)
    return result


def rgb_to_yuv(rgb: np.ndarray) -> np.ndarray:
    frame = av.VideoFrame.from_ndarray(rgb, format="rgb24")
    return VideoReformatter().reformat(frame, format="yuv420p", src_colorspace="ITU709",
        dst_colorspace="ITU709", src_color_range="JPEG", dst_color_range="MPEG").to_ndarray()


def yuv_frame(yuv: np.ndarray) -> av.VideoFrame:
    frame = av.VideoFrame.from_ndarray(np.ascontiguousarray(yuv), format="yuv420p")
    frame.colorspace, frame.color_range = 1, 1
    return frame


def yuv_to_rgb(yuv: np.ndarray) -> np.ndarray:
    return VideoReformatter().reformat(yuv_frame(yuv), format="rgb24", src_colorspace="ITU709",
        dst_colorspace="ITU709", src_color_range="MPEG", dst_color_range="JPEG").to_ndarray()


def psnr(mse: float) -> float | None:
    # JSON null plus mse=0 identifies exact equality; never serialize Infinity.
    return float(10 * np.log10(255.0 ** 2 / mse)) if mse > 0 else None


def ssim_map(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Wang-style single-scale luma SSIM, 11x11 Gaussian sigma1.5, range255.

    Population moments, no downsampling, valid-window mean (5px border removed
    by each region). This is not multiscale SSIM or a stereo/perceptual score.
    """
    a, b = a.astype(np.float32), b.astype(np.float32)
    blur = lambda v: cv2.GaussianBlur(v, (11, 11), 1.5, borderType=cv2.BORDER_REFLECT_101)
    ma, mb = blur(a), blur(b)
    va, vb = np.maximum(blur(a * a) - ma * ma, 0), np.maximum(blur(b * b) - mb * mb, 0)
    covariance = blur(a * b) - ma * mb
    value = ((2 * ma * mb + 6.5025) * (2 * covariance + 58.5225)) / ((ma * ma + mb * mb + 6.5025) * (va + vb + 58.5225))
    return np.minimum(value, 1.0)


def quality(reference_rgb: np.ndarray, decoded_rgb: np.ndarray, area: dict,
            reference_y: np.ndarray | None = None, decoded_y: np.ndarray | None = None) -> dict:
    if reference_rgb.shape != decoded_rgb.shape:
        raise ValueError("Decoded shape differs from the paired reference")
    if reference_y is None:
        weights = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
        reference_y = reference_rgb.astype(np.float32) @ weights
        decoded_y = decoded_rgb.astype(np.float32) @ weights
    if decoded_y is None or reference_y.shape != decoded_y.shape:
        raise ValueError("Paired luma planes required")
    similarity = ssim_map(reference_y, decoded_y)
    rgb_error = reference_rgb.astype(np.float32) - decoded_rgb.astype(np.float32)
    luma_error = reference_y.astype(np.float32) - decoded_y.astype(np.float32)
    gradient = lambda v: (cv2.Sobel(v.astype(np.float32), cv2.CV_32F, 1, 0, ksize=3, scale=1/8),
                          cv2.Sobel(v.astype(np.float32), cv2.CV_32F, 0, 1, ksize=3, scale=1/8))
    ax, ay = gradient(reference_y)
    bx, by = gradient(decoded_y)
    edge_error = np.hypot(ax - bx, ay - by)
    result = {}
    for name, (x, y, width, height) in area.items():
        crop = np.s_[y:y + height, x:x + width]
        rgb_mse = float(np.mean(rgb_error[crop] ** 2, dtype=np.float64))
        y_mse = float(np.mean(luma_error[crop] ** 2, dtype=np.float64))
        result[name] = {"rgb_mse": rgb_mse, "rgb_psnr_db": psnr(rgb_mse),
                       "luma_mse": y_mse, "luma_psnr_db": psnr(y_mse),
                       "luma_ssim": float(similarity[y+5:y+height-5, x+5:x+width-5].mean(dtype=np.float64)),
                       "gradient_residual_mae": float(edge_error[y+1:y+height-1, x+1:x+width-1].mean(dtype=np.float64))}
    return result


def summarize_quality(rows: list[dict], field: str) -> dict:
    result = {}
    for region in rows[0][field]:
        metrics = [row[field][region] for row in rows]
        pooled = {key: float(np.mean([m[key] for m in metrics]))
                  for key in ("rgb_mse", "luma_mse", "luma_ssim", "gradient_residual_mae")}
        pooled.update(rgb_psnr_db=psnr(pooled["rgb_mse"]), luma_psnr_db=psnr(pooled["luma_mse"]),
                      minimum_frame_luma_ssim=min(m["luma_ssim"] for m in metrics))
        result[region] = pooled
    return result


def prepare(sequence: dict, output: Path) -> dict:
    width, height = sequence["packed_size"]
    matrix = np.lib.format.open_memmap(output / "common-yuv420p.npy", mode="w+", dtype=np.uint8,
                                     shape=(len(sequence["frames"]), height * 3 // 2, width))
    area, metrics = regions(sequence), []
    try:
        for row in sequence["frames"]:
            rgb = read_rgb(row)
            if rgb.shape != (height, width, 3):
                raise ValueError("Source geometry changed during preparation")
            matrix[row["slot"]] = rgb_to_yuv(rgb)
            roundtrip = yuv_to_rgb(matrix[row["slot"]])
            metrics.append({"slot": row["slot"], "frame_id": row["frame_id"],
                            "quality": quality(rgb, roundtrip, area)})
        matrix.flush()
    finally:
        del matrix
    result = {"format": "yuv420p", "matrix": "Rec709", "range": "limited", "transfer_assumption":
              "source RGB code values preserved; no gamma-linearization or transfer conversion",
              "file": "common-yuv420p.npy", "sha256": sha256(output / "common-yuv420p.npy"),
              "rgb_to_420_roundtrip": summarize_quality(metrics, "quality"), "frames": metrics}
    write_json(output / "preparation.json", result)
    return result


def encoder_options(codec: str, preset: str, bitrate_kbps: int, fps: Fraction) -> dict:
    if codec not in ("h264", "hevc") or preset not in ("p1", "p2", "p3"):
        raise ValueError("Only H264/HEVC 8-bit and P1/P2/P3 are in this comparison")
    if bitrate_kbps <= 0 or not 1 <= fps <= 240:
        raise ValueError("Positive bitrate and FPS in [1,240] required")
    return {"preset": preset, "tune": "ull", "profile": "high" if codec == "h264" else "main",
            "rc": "cbr", "multipass": "qres", "rc-lookahead": "0", "zerolatency": "1",
            "delay": "0", "spatial-aq": "0", "temporal-aq": "0", "ldkfs": "1",
            "bufsize": str(int(bitrate_kbps * 1000 / fps)), "maxrate": str(bitrate_kbps * 1000),
            "forced-idr": "1"}


def summary_ms(values: list[float]) -> dict:
    return {"count": len(values), "p50": float(np.percentile(values, 50)),
            "p95": float(np.percentile(values, 95)), "maximum": max(values), "total": sum(values)}


def encode_case(matrix: np.ndarray, sequence: dict, directory: Path, codec: str,
                preset: str, bitrate_kbps: int, fps: Fraction) -> dict:
    """Only this function opens NVENC. The caller owns authorization/GPU scheduling."""
    width, height = sequence["packed_size"]
    options = encoder_options(codec, preset, bitrate_kbps, fps)
    encoder = av.CodecContext.create(f"{codec}_nvenc", "w")
    encoder.width, encoder.height, encoder.pix_fmt = width, height, "yuv420p"
    encoder.time_base, encoder.framerate = 1 / fps, fps
    encoder.bit_rate, encoder.gop_size, encoder.max_b_frames = bitrate_kbps * 1000, 2147483647, 0
    encoder.colorspace, encoder.color_range = 1, 1
    encoder.color_primaries, encoder.color_trc = 1, 1
    encoder.options = options.copy()
    packets, calls, submitted = [], [], {}
    start = time.perf_counter_ns()
    result = {"codec": codec, "preset": preset, "bitrate_kbps_requested": bitrate_kbps,
              "fps": str(fps), "options_requested": options, "status": "failed"}
    try:
        with av.logging.Capture() as logs:
            opening = time.perf_counter_ns()
            encoder.open()
            result["open_ms"] = (time.perf_counter_ns() - opening) / 1e6
            result["options_not_consumed"] = dict(encoder.options)
            if encoder.options:
                raise RuntimeError(f"Encoder did not consume requested options: {encoder.options}")
            result["context_after_open"] = {"name": encoder.name, "profile": encoder.profile,
                "pix_fmt": encoder.pix_fmt, "time_base": str(encoder.time_base), "framerate": str(encoder.framerate),
                "bit_rate": encoder.bit_rate, "max_bit_rate": encoder.max_bit_rate,
                "has_b_frames": encoder.has_b_frames, "gop_size": encoder.gop_size,
                "color_range": encoder.color_range, "colorspace": encoder.colorspace,
                "color_primaries": encoder.color_primaries, "color_trc": encoder.color_trc}
            with (directory / f"stream.{codec}").open("xb") as stream:
                def save(returned, returned_ns, flush):
                    for packet in returned:
                        if packet.pts is None or packet.pts not in submitted:
                            raise RuntimeError("Encoded packet lacks a matching submitted PTS")
                        raw = bytes(packet)
                        packets.append({"pts": packet.pts, "dts": packet.dts, "time_base": str(packet.time_base or 1/fps),
                            "offset": stream.tell(), "size": len(raw), "is_keyframe": packet.is_keyframe,
                            "returned_on_flush": flush, "packet_return_ms": (returned_ns - submitted[packet.pts]) / 1e6,
                            "return_after_submit_slots": len(submitted) - 1 - packet.pts})
                        stream.write(raw)
                for row in sequence["frames"]:
                    frame = yuv_frame(matrix[row["slot"]])
                    frame.pts, frame.time_base = row["slot"], 1 / fps
                    submitted[row["slot"]] = time.perf_counter_ns()
                    returned = encoder.encode(frame)
                    now = time.perf_counter_ns()
                    calls.append((now - submitted[row["slot"]]) / 1e6)
                    save(returned, now, False)
                flush_start = time.perf_counter_ns()
                returned = encoder.encode(None)
                now = time.perf_counter_ns()
                result["flush_ms"] = (now - flush_start) / 1e6
                save(returned, now, True)
            if sorted(set(p["pts"] for p in packets)) != list(range(len(sequence["frames"]))):
                raise RuntimeError("Encoded packets do not cover every input PTS")
            result.update(status="encoded", encode_call_ms=summary_ms(calls),
                          packet_return_ms=summary_ms([p["packet_return_ms"] for p in packets]))
    finally:
        # PyAV15 CodecContext has no close() API; releasing the sole context frees
        # this synchronous encoder. No worker or independently retained GPU tensor.
        del encoder
        result["wall_open_encode_release_ms"] = (time.perf_counter_ns() - start) / 1e6
        result["ffmpeg_logs"] = [{"level": x[0], "component": x[1], "message": x[2]} for x in locals().get("logs", [])]
        result["packets"] = packets
        write_json(directory / "encode.json", result)
    path = directory / f"stream.{codec}"
    result.update(bitstream_sha256=sha256(path), bitstream_bytes=path.stat().st_size,
                  nominal_duration_seconds=float(len(sequence["frames"]) / fps),
                  actual_elementary_mbps=path.stat().st_size * 8 / float(len(sequence["frames"]) / fps) / 1e6)
    return result


def decode_case(sequence: dict, matrix: np.ndarray, directory: Path, encoded: dict) -> dict:
    """Software decode the saved elementary packet bytes, with saved encoder PTS.

    PTS are an external fixture pairing ledger, not timestamps extracted from an
    RTP stream or elementary H264/HEVC file. No frame-order-only pairing fallback.
    """
    decoder = av.CodecContext.create(encoded["codec"], "r")
    decoder.thread_count = 1
    frames, timings, observed_pts, observations = [], [], [], []
    width, height = sequence["packed_size"]
    area = regions(sequence)
    whole_start = time.perf_counter_ns()
    def consume(decoded):
        for frame in decoded:
            slot = frame.pts
            if slot != len(frames) or slot in observed_pts or slot >= len(sequence["frames"]):
                raise ValueError(f"Missing, reordered or invalid decoded PTS: {slot}")
            if (frame.width, frame.height) != (width, height) or frame.format.name != "yuv420p":
                raise ValueError("Unexpected decoded visible dimensions or non-8-bit-420 format")
            if (frame.colorspace, frame.color_range) != (1, 1):
                raise ValueError("Bitstream did not signal expected Rec709 limited color metadata")
            observations.append({"profile": decoder.profile, "pix_fmt": frame.format.name,
                "context_pix_fmt": decoder.pix_fmt, "visible_size": [frame.width, frame.height],
                "coded_size": [decoder.coded_width, decoder.coded_height], "has_b_frames": decoder.has_b_frames})
            observed_pts.append(slot)
            yuv = frame.to_ndarray()
            actual_rgb = yuv_to_rgb(yuv)
            ref_rgb = read_rgb(sequence["frames"][slot])
            common_rgb = yuv_to_rgb(matrix[slot])
            metrics = {"slot": slot, "frame_id": sequence["frames"][slot]["frame_id"],
                "visible_size": [frame.width, frame.height], "pix_fmt": frame.format.name,
                "colorspace": frame.colorspace, "color_range": frame.color_range,
                "source_rgb": quality(ref_rgb, actual_rgb, area),
                "codec_only": quality(common_rgb, actual_rgb, area, matrix[slot, :height], yuv[:height])}
            frames.append(metrics)
            if slot in {0, len(sequence["frames"]) // 2, len(sequence["frames"]) - 1}:
                Image.fromarray(actual_rgb).save(directory / f"decoded-{slot:05d}.png")
    try:
        with (directory / f"stream.{encoded['codec']}").open("rb") as stream:
            for row in encoded["packets"]:
                stream.seek(row["offset"])
                raw = stream.read(row["size"])
                if len(raw) != row["size"]:
                    raise ValueError("Truncated saved elementary stream")
                packet = av.Packet(raw)
                packet.pts, packet.dts, packet.time_base = row["pts"], row["dts"], Fraction(row["time_base"])
                started = time.perf_counter_ns()
                decoded = decoder.decode(packet)
                timings.append((time.perf_counter_ns() - started) / 1e6)
                consume(decoded)
            started = time.perf_counter_ns()
            decoded = decoder.decode(None)
            timings.append((time.perf_counter_ns() - started) / 1e6)
            consume(decoded)
        if len(frames) != len(sequence["frames"]):
            raise ValueError("Decoded frame count does not match the exact paired sequence")
        return {"status": "decoded_and_measured", "frames": frames,
            "source_rgb": summarize_quality(frames, "source_rgb"), "codec_only": summarize_quality(frames, "codec_only"),
            "decoder": {"name": decoder.name, **observations[-1],
                "crop_offsets": None, "crop_reason": "Not exposed by this PyAV API; no offsets inferred",
                "thread_count": decoder.thread_count},
            "decode_call_ms": summary_ms(timings), "decode_analysis_wall_ms": (time.perf_counter_ns() - whole_start) / 1e6}
    finally:
        del decoder


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sequence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fps", type=Fraction, required=True, help="Exact nominal encoder FPS, e.g. 72 or 60000/1001")
    parser.add_argument("--bitrate-kbps", default="53000", help="Comma-separated targets; each is a separate case")
    parser.add_argument("--codecs", default="h264,hevc")
    parser.add_argument("--presets", default="p1,p3")
    parser.add_argument("--prepare-only", action="store_true", help="CPU validation and common input only; never open NVENC")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    output = args.output.resolve()
    if output.exists():
        raise ValueError("Output already exists; choose a NEW directory to preserve all prior evidence")
    if not output.is_relative_to(ROOT / "artifacts"):
        raise ValueError("Keep private comparison output under the workspace artifacts directory")
    codecs, presets = args.codecs.split(","), args.presets.split(",")
    bitrates = [int(value) for value in args.bitrate_kbps.split(",")]
    for values in (codecs, presets, bitrates):
        if len(set(values)) != len(values):
            raise ValueError("Repeated case choices are not allowed")
    for codec in codecs:
        for preset in presets:
            for bitrate in bitrates:
                encoder_options(codec, preset, bitrate, args.fps)
    sequence = load_sequence(args.sequence)
    output.mkdir(parents=True, exist_ok=False)
    result = {"schema": SCHEMA, "status": "preparing", "created_utc": datetime.now(timezone.utc).isoformat(),
              "argv": list(argv if argv is not None else sys.argv[1:]), "script_sha256": sha256(Path(__file__)),
              "python": sys.version, "python_executable": sys.executable, "python_executable_sha256": sha256(Path(sys.executable)),
              "platform": platform.platform(), "pyav_version": av.__version__, "ffmpeg_library_versions": av.library_versions,
              "sequence": sequence, "limitations": LIMITATIONS, "gpu_encode_requested": not args.prepare_only,
              "fps": str(args.fps), "cases": []}
    write_json(output / "sequence-resolved.json", sequence)
    try:
        result["preparation"] = prepare(sequence, output)
        if args.prepare_only:
            result["status"] = "prepared_cpu_only"
            return 0
        matrix = np.load(output / "common-yuv420p.npy", mmap_mode="r")
        try:
            for codec in codecs:
                for preset in presets:
                    for bitrate in bitrates:
                        name = f"{codec}-{preset}-{bitrate}kbps"
                        directory = output / name
                        directory.mkdir()
                        case = {"name": name, "status": "failed"}
                        result["cases"].append(case)
                        try:
                            case["encoded"] = encode_case(matrix, sequence, directory, codec, preset, bitrate, args.fps)
                            case["decoded"] = decode_case(sequence, matrix, directory, case["encoded"])
                            case["status"] = "measured_offline"
                        except Exception as error:
                            case["error"] = f"{type(error).__name__}: {error}"
                        write_json(directory / "result.json", case)
        finally:
            del matrix
        result["status"] = "measured_offline" if all(c["status"] == "measured_offline" for c in result["cases"]) else "failed"
        return 0 if result["status"] == "measured_offline" else 1
    except BaseException as error:
        result.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        write_json(output / "result.json", result)


if __name__ == "__main__":
    raise SystemExit(main())
