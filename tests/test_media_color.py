"""Actual CPU codec/color regressions; no desktop, CUDA, network or Quest IO."""
from fractions import Fraction
from types import SimpleNamespace

import av
import numpy as np
from PIL import Image
import pytest

from quest3d.media import (FileVideoSource, MediaReader, UnsupportedMediaColor,
                           video_color_policy)


def color_frame(matrix=1, color_range=1):
    """Make raw YUV independently of libswscale, including neutral endpoints."""
    neutral = [16, 64, 128, 235] if color_range == 1 else [0, 64, 128, 255]
    tiles = [(level, 128, 128) for level in neutral]
    tiles += [(100, 110, 180), (160, 80, 90), (70, 190, 120), (200, 135, 145)]
    yuv = np.repeat(np.array(tiles, np.uint8)[None, :, :], 16, axis=0)
    yuv = np.repeat(yuv, 16, axis=1)
    frame = av.VideoFrame(yuv.shape[1], yuv.shape[0], "yuv444p")
    for channel, plane in enumerate(frame.planes):
        padded = np.zeros((plane.height, plane.line_size), np.uint8)
        padded[:, :plane.width] = yuv[:, :, channel]
        plane.update(padded)
    frame.colorspace, frame.color_range = matrix, color_range
    return frame, yuv


def expected_rgb(yuv, matrix, color_range):
    # ITU matrix arithmetic is independent of the implementation's PyAV call.
    kr, kb = (0.2126, 0.0722) if matrix == 1 else (0.299, 0.114)
    values = yuv.astype(np.float64)
    y = (values[:, :, 0] - (16 if color_range == 1 else 0)) / (219 if color_range == 1 else 255)
    cb = (values[:, :, 1] - 128) / (224 if color_range == 1 else 255)
    cr = (values[:, :, 2] - 128) / (224 if color_range == 1 else 255)
    red = y + 2 * (1 - kr) * cr
    blue = y + 2 * (1 - kb) * cb
    green = y - 2 * kb * (1 - kb) / (1 - kr - kb) * cb - 2 * kr * (1 - kr) / (1 - kr - kb) * cr
    return np.clip(np.stack([red, green, blue], axis=2) * 255, 0, 255)


def write_clip(path, matrix=1, color_range=1, transfer=1, primaries=1):
    with av.open(str(path), "w") as output:
        stream = output.add_stream("ffv1", rate=24)
        stream.width, stream.height, stream.pix_fmt = 128, 16, "yuv444p"
        context = stream.codec_context
        context.colorspace, context.color_range = matrix, color_range
        context.color_trc, context.color_primaries = transfer, primaries
        for number in range(2):
            frame, yuv = color_frame(matrix, color_range)
            frame.pts, frame.time_base = number, Fraction(1, 24)
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
    return yuv


@pytest.mark.parametrize("matrix", [1, 5, 6])
@pytest.mark.parametrize("color_range", [1, 2])
def test_real_lossless_clip_uses_declared_matrix_and_range(tmp_path, matrix, color_range):
    path = tmp_path / f"color-{matrix}-{color_range}.mkv"
    yuv = write_clip(path, matrix, color_range)
    reader = MediaReader(path)
    try:
        pixels, pts = reader.next()
        reference = expected_rgb(yuv, matrix, color_range)
        error = np.abs(pixels[:, :, [2, 1, 0]].astype(float) - reference)
        assert error.max() <= 2, (matrix, color_range, float(error.max()))
        assert np.all(pixels[:, :, 3] == 255)
        assert pixels.flags.c_contiguous and pts == 0
        assert reader.color_info["matrix_metadata"] == matrix
        assert reader.color_info["range_metadata"] == color_range
        assert reader.color_info["source_range"] == ("MPEG" if color_range == 1 else "JPEG")
        assert not reader.color_info["assumptions"]
        frozen = pixels.copy()
        _, next_pts = reader.next()
        assert next_pts > pts and np.array_equal(pixels, frozen)
        reader.seek(0)
        again, pts = reader.next()
        assert pts == 0 and np.array_equal(again, frozen)
    finally:
        reader.close()


@pytest.mark.parametrize("color_range", [1, 2])
def test_709_default_decode_error_is_reproduced_and_fixed(tmp_path, color_range):
    path = tmp_path / f"default-error-{color_range}.mkv"
    yuv = write_clip(path, 1, color_range)
    reference = expected_rgb(yuv, 1, color_range)
    with av.open(str(path)) as container:
        default = next(container.decode(video=0)).to_ndarray(format="bgra")
    reader = MediaReader(path)
    try:
        actual, _ = reader.next()
    finally:
        reader.close()
    old_error = np.abs(default[:, :, [2, 1, 0]].astype(float) - reference).mean()
    new_error = np.abs(actual[:, :, [2, 1, 0]].astype(float) - reference).mean()
    assert old_error > 3, "Fixture must actually reveal the old default-matrix/range fault"
    assert new_error < 1 and new_error * 3 < old_error


def test_unknown_yuv_policy_is_explicit_601_limited_without_resolution_guess(caplog, tmp_path):
    path = tmp_path / "unknown.mkv"
    write_clip(path, matrix=2, color_range=0, transfer=2, primaries=2)
    reader = MediaReader(path)
    try:
        reader.next()
        assert reader.color_info["source_matrix"] == "ITU601"
        assert reader.color_info["source_range"] == "MPEG"
        assert any("assume BT.601" in line for line in reader.color_info["assumptions"])
        assert "File color assumptions" in caplog.text
        count = len(caplog.records)
        reader.next()
        assert len(caplog.records) == count, "Do not log the same assumptions once per video frame"
    finally:
        reader.close()
    large = av.VideoFrame(1920, 1080, "yuv420p")
    assert video_color_policy(large).source_matrix == "ITU601"


def test_explicit_frame_metadata_is_used_after_each_decode():
    frame, _ = color_frame(1, 1)
    context = SimpleNamespace(color_trc=1, color_primaries=1)
    first = video_color_policy(frame, context)
    frame.colorspace, frame.color_range = 6, 2
    second = video_color_policy(frame, context)
    assert first.source_matrix == "ITU709" and first.source_range == "MPEG"
    assert second.source_matrix == "ITU601" and second.source_range == "JPEG"
    assert frame.colorspace == 6 and frame.color_range == 2


@pytest.mark.parametrize("transfer,primaries,matrix", [(16, 1, 1), (18, 1, 1),
    (1, 9, 1), (1, 1, 9), (1, 1, 10), (1, 1, 14), (8, 1, 1), (9, 1, 1),
    (1, 12, 1), (1, 1, 4), (1, 1, 7), (1, 1, 0)])
def test_unsupported_hdr_gamut_transfer_or_matrix_is_not_silently_sdr(transfer, primaries, matrix):
    frame, _ = color_frame(matrix, 1)
    with pytest.raises(UnsupportedMediaColor):
        video_color_policy(frame, SimpleNamespace(color_trc=transfer, color_primaries=primaries))


@pytest.mark.parametrize("transfer", [16, 18])
def test_actual_hdr_tagged_clip_is_rejected_before_pixels_are_returned(tmp_path, transfer):
    path = tmp_path / f"hdr-{transfer}.mkv"
    write_clip(path, transfer=transfer)
    reader = MediaReader(path)
    try:
        with pytest.raises(UnsupportedMediaColor, match="HDR"):
            reader.next()
    finally:
        reader.close()


def test_unknown_high_depth_and_limited_rgb_are_explicitly_unsupported():
    with pytest.raises(UnsupportedMediaColor, match="unspecified transfer"):
        video_color_policy(av.VideoFrame(16, 16, "yuv420p10le"))
    rgb = av.VideoFrame(16, 16, "bgra")
    rgb.color_range = 1
    with pytest.raises(UnsupportedMediaColor, match="Limited-range RGB"):
        video_color_policy(rgb)
    frame, _ = color_frame()
    frame.color_range = 3
    with pytest.raises(UnsupportedMediaColor, match="color range"):
        video_color_policy(frame)


def test_rgb_values_and_jpeg_pixel_format_do_not_get_guessed_yuv_conversion():
    rgb = av.VideoFrame.from_ndarray(np.full((4, 4, 4), (19, 53, 231, 255), np.uint8), format="bgra")
    policy = video_color_policy(rgb)
    assert policy.source_matrix == "RGB" and policy.source_range == "JPEG"
    assert np.all(rgb.to_ndarray(**policy.reformat_options()) == (19, 53, 231, 255))
    jpeg = av.VideoFrame(16, 16, "yuvj420p")
    assert video_color_policy(jpeg).source_range == "JPEG"


def test_photo_rgb_alpha_and_unmanaged_icc_are_reported(tmp_path):
    path = tmp_path / "rgba.png"
    Image.new("RGBA", (4, 4), (200, 100, 40, 128)).save(path, icc_profile=b"fixture-unsupported-profile")
    with FileVideoSource(path, paused=True) as source:
        frame = source.grab()
        assert np.all(frame.bgra == (20, 50, 100, 255))
        color = source.playback_status()["color"]
        assert color["icc_present"] and not color["icc_applied"]
        assert "Pillow RGB" in color["conversion"]


def test_high_bit_depth_photo_is_not_clipped_into_an_unmarked_sdr_image(tmp_path):
    path = tmp_path / "linear.tif"
    Image.fromarray(np.full((4, 4), 12000, np.uint16)).save(path)
    with pytest.raises(UnsupportedMediaColor, match="photo"):
        MediaReader(path)
