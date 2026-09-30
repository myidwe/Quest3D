from dataclasses import fields
import os
from pathlib import Path

import numpy as np
import pytest

from quest3d.stereo import StereoFrame
from quest3d.subtitles import FlatSubtitleRenderer, SubtitleCue, SubtitleTrack
import quest3d.subtitles as subtitles


SRT = """1
00:00:01,000 --> 00:00:03,000
첫 번째 자막

2
00:00:02,000 --> 00:00:04,000
두 번째 자막
겹치는 줄

3
00:00:04,000 --> 00:00:05,000
마지막 자막
"""


@pytest.fixture
def font_path():
    path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/malgun.ttf"
    if not path.is_file():
        pytest.skip("Requires the installed Windows Malgun Gothic font; no font is redistributed")
    return path


@pytest.fixture
def renderer(font_path):
    return FlatSubtitleRenderer(font_path, 28, margin_px=16, padding_px=6)


@pytest.fixture
def stereo():
    image = np.empty((480, 1280, 4), dtype=np.uint8)
    image[:, :640] = (140, 70, 15, 255)
    image[:, 640:] = (20, 100, 180, 255)
    return image


def test_utf8_bom_crlf_and_exact_boundaries(tmp_path):
    path = tmp_path / "한글.srt"
    path.write_bytes(b"\xef\xbb\xbf" + SRT.replace("\n", "\r\n").encode("utf-8"))
    track = SubtitleTrack.load(path)
    expected = {
        0: (), 999999999: (), 1000000000: (1,), 1999999999: (1,),
        2000000000: (1, 2), 2999999999: (1, 2), 3000000000: (2,),
        3999999999: (2,), 4000000000: (3,), 5000000000: (),
    }
    for pts, indices in expected.items():
        assert tuple(cue.index for cue in track.active(pts)) == indices
    assert not track.active(2000000000, eof=True)
    assert track.cues[0].text == "첫 번째 자막"


def test_backward_seek_overlap_file_order_and_no_wall_clock(monkeypatch):
    cues = [SubtitleCue(9, 20, 90, "later in time, first in file"), SubtitleCue(2, 0, 100, "long"),
            SubtitleCue(3, 10, 30, "short")]
    track = SubtitleTrack(cues)
    for pts in (25, 99, 25, 0, 100, 20, 30, 29, 25):
        assert track.active(pts) == tuple(cue for cue in cues if cue.start_ns <= pts < cue.end_ns)
    import time
    def forbidden():
        raise AssertionError("Subtitle lookup must never read wall or monotonic time")
    monkeypatch.setattr(time, "time", forbidden)
    monkeypatch.setattr(time, "monotonic", forbidden)
    assert track.active(25) == tuple(cues)


def test_interval_index_matches_linear_oracle_with_long_and_nested_cues():
    cues = [SubtitleCue(1, 0, 100000, "long")]
    cues += [SubtitleCue(i + 2, i * 100, i * 100 + 50, str(i)) for i in range(500)]
    track = SubtitleTrack(cues)
    random = np.random.default_rng(8)
    for pts in random.integers(0, 100001, 300):
        assert track.active(int(pts)) == tuple(c for c in cues if c.start_ns <= pts < c.end_ns)


@pytest.mark.parametrize("text", [
    "", "1\n00:00:01,000 --> 00:00:01,000\na", "1\n00:00:03,000 --> 00:00:01,000\na",
    "1\n00:60:00,000 --> 01:01:00,000\na", "1\n00:00:01.000 --> 00:00:02,000\na",
    "1\n00:00:01,000 --> 00:00:02,000", "x\n00:00:01,000 --> 00:00:02,000\na",
    "1\n00:00:01,000 --> 00:00:02,000\na\x00", SRT + "\n" + SRT,
    "1\n00:00:01,000 --> 00:00:02,000\n\ud800",
])
def test_malformed_srt_is_explicit(text):
    with pytest.raises(ValueError):
        SubtitleTrack.from_text(text)


def test_file_encoding_and_limits(tmp_path, monkeypatch):
    path = tmp_path / "bad.srt"
    path.write_bytes(SRT.encode("cp949"))
    with pytest.raises(ValueError, match="UTF-8"):
        SubtitleTrack.load(path)
    monkeypatch.setattr(subtitles, "MAX_FILE_BYTES", 128)
    path.write_bytes(b"x" * 129)
    with pytest.raises(ValueError, match="exceeds"):
        SubtitleTrack.load(path)
    with pytest.raises(ValueError, match="exceeds"):
        SubtitleTrack.from_text("가" * 44)


def test_bounded_overlap_text_and_iterator(monkeypatch):
    with pytest.raises(ValueError, match="overlapping"):
        SubtitleTrack(SubtitleCue(i + 1, 0, 9, "a") for i in range(9))
    with pytest.raises(ValueError, match="oversized"):
        SubtitleTrack([SubtitleCue(1, 0, 9, "a" * 2049)])
    monkeypatch.setattr(subtitles, "MAX_CUES", 2)
    seen = []
    def endless():
        for i in range(10000):
            seen.append(i)
            yield SubtitleCue(i + 1, i, i + 1, "a")
    with pytest.raises(ValueError, match="cues"):
        SubtitleTrack(endless())
    assert len(seen) == 3


@pytest.mark.parametrize("pts", [-1, True, 1.5, None, 1 << 63])
def test_invalid_pts(pts):
    with pytest.raises(ValueError):
        SubtitleTrack.from_text(SRT).active(pts)


def test_multiple_separators_and_multiline():
    track = SubtitleTrack.from_text(SRT.replace("\n\n", "\n\n \n\n"))
    assert len(track.cues) == 3 and track.cues[1].text == "두 번째 자막\n겹치는 줄"


def test_flat_pixels_outside_preserved_original_immutable(renderer, stereo):
    original = stereo.copy()
    track = SubtitleTrack.from_text(SRT)
    result = renderer.composite(stereo, 2000000000, track)
    assert np.array_equal(stereo, original) and not np.shares_memory(stereo, result)
    x, y, w, h = renderer.last_diagnostics["overlay_rect"]
    assert renderer.last_diagnostics["cue_indices"] == [1, 2]
    assert h <= stereo.shape[0] // 3 and w < stereo.shape[1] // 2
    assert np.array_equal(result[y:y+h, x:x+w], result[y:y+h, 640+x:640+x+w])
    outside = np.ones(stereo.shape[:2], dtype=bool)
    outside[y:y+h, x:x+w] = False
    outside[y:y+h, 640+x:640+x+w] = False
    assert np.array_equal(result[outside], original[outside])
    assert result[y:y+h, x:x+w, :3].max() == 255
    assert np.all(result[y:y+h, x:x+w, 3] == 255)


def test_hidden_eof_no_cue_and_resize(renderer, stereo):
    track = SubtitleTrack.from_text(SRT)
    for pts, eof in ((0, False), (5000000000, False), (2000000000, True)):
        output = renderer.composite(stereo, pts, track, eof=eof)
        assert np.array_equal(output, stereo) and not np.shares_memory(output, stereo)
    renderer.set_visible(False)
    assert np.array_equal(renderer.composite(stereo, 2000000000, track), stereo)
    renderer.set_visible(True)
    renderer.composite(stereo, 1000000000, track)
    small = renderer.last_diagnostics["overlay_rect"]
    renderer.set_size(40)
    renderer.composite(stereo, 1000000000, track)
    large = renderer.last_diagnostics["overlay_rect"]
    assert large[2] > small[2] and large[3] > small[3]
    with pytest.raises(ValueError): renderer.set_size(True)
    with pytest.raises(ValueError): renderer.set_size(200)
    with pytest.raises(ValueError): renderer.set_visible(1)


def test_stereo_frame_metadata_and_noncontiguous_input(renderer, stereo):
    source = stereo[:, ::-1]
    frame = StereoFrame(source, 17, 8, "3d", True, (0.2, 0.9), (20, 10, 500, 450))
    result = renderer.composite_frame(frame, 1000000000, SubtitleTrack.from_text(SRT))
    for field in fields(frame):
        if field.name != "bgra":
            assert getattr(result, field.name) == getattr(frame, field.name)
    assert result.bgra.flags.c_contiguous and not np.shares_memory(result.bgra, source)
    assert np.array_equal(frame.bgra, stereo[:, ::-1])


def test_real_korean_font_and_missing_glyph(renderer, stereo, tmp_path):
    assert len(renderer._coverage.tables) == 1
    for char in "한글가나다":
        assert renderer._coverage.contains(char)
    assert not renderer._coverage.contains(chr(0x10ffff))
    font = renderer._font()
    assert bytes(font.getmask("한")) != bytes(font.getmask("글"))
    track = SubtitleTrack([SubtitleCue(1, 0, 100, "한글 자막: 안녕하세요!\nWindows 설치 글꼴 사용")])
    output = renderer.composite(stereo, 1, track)
    from PIL import Image
    path = tmp_path / "actual-korean-subtitles.png"
    Image.fromarray(output[:, :, [2, 1, 0, 3]], "RGBA").save(path)
    print(f"Actual installed-font BGRA render: {path}")
    with pytest.raises(ValueError, match="lacks glyph"):
        renderer.composite(stereo, 1, SubtitleTrack([SubtitleCue(1, 0, 2, chr(0x10ffff))]))
    assert "error" in renderer.last_diagnostics and renderer.last_diagnostics["pts_ns"] == 1


def test_odd_eye_width_korean_wrap_and_cache(font_path):
    renderer = FlatSubtitleRenderer(font_path, 20, margin_px=8, cache_bytes=100000)
    image = np.zeros((360, 642, 4), np.uint8)
    image[:, :, 3] = 255
    track = SubtitleTrack([SubtitleCue(1, 0, 100, "긴 한국어 문장을 눈 화면 안에 자동으로 줄바꿈합니다")])
    first = renderer.composite(image, 1, track)
    again = renderer.composite(image, 1, track)
    assert renderer.last_diagnostics["cache_hit"] and np.array_equal(first, again)
    assert np.array_equal(first[:, :321], first[:, 321:])
    for size in (16, 18, 20, 22, 24, 26):
        renderer.set_size(size)
        for i in range(20):
            renderer.composite(image, 1, SubtitleTrack([SubtitleCue(1, 0, 2, f"자막 {i}")]))
            assert renderer.last_diagnostics["cache_bytes"] <= 100000
            assert renderer.last_diagnostics["cache_entries"] <= 16
            assert renderer.last_diagnostics["font_cache_entries"] <= 2


def test_oversized_layout_and_wrong_image_fail_without_mutation(renderer, stereo):
    source = stereo.copy()
    with pytest.raises(ValueError, match="lines|third"):
        renderer.composite(stereo, 1, SubtitleTrack([SubtitleCue(1, 0, 2, "가\n" * 30)]))
    assert np.array_equal(stereo, source)
    for bad in (np.zeros((20, 33, 4), np.uint8), np.zeros((20, 40, 3), np.uint8), stereo.astype(float)):
        with pytest.raises(ValueError):
            renderer.composite(bad, 1, SubtitleTrack.from_text(SRT))


def test_invalid_and_oversized_font(tmp_path, font_path, monkeypatch):
    path = tmp_path / "bad.ttf"
    path.write_bytes(b"not a font")
    with pytest.raises(ValueError):
        FlatSubtitleRenderer(path)
    monkeypatch.setattr(subtitles, "MAX_FONT_BYTES", 100)
    with pytest.raises(ValueError, match="Font exceeds"):
        FlatSubtitleRenderer(font_path)
