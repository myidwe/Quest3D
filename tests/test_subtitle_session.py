"""File decode/scheduling caption regressions; injected AI is not model proof."""
import json
from types import SimpleNamespace

import numpy as np
import pytest

from quest3d import file_session, session
from quest3d.subtitle_session import FileSubtitleOverlay, validate_subtitle_options
from quest3d.stereo import StereoFrame
from test_file_session import setup
from test_session import _synthetic_depth
from test_media import video


FONT = "C:/Windows/Fonts/malgun.ttf"


def captions(tmp_path, text="1\n00:00:00,000 --> 00:00:00,200\n처음\n\n2\n00:00:00,300 --> 00:00:00,600\n다음\n"):
    path = tmp_path / "captions.srt"
    path.write_text(text, encoding="utf-8")
    return str(path)


class Fast:
    def __init__(self, _):
        pass
    def infer(self, bgra, *, frame_id, generation):
        return _synthetic_depth(bgra, frame_id, generation)


@pytest.mark.parametrize("options", [dict(subtitles="x.srt"), dict(subtitle_font=FONT),
    dict(subtitle_size=32), dict(file="x.mp4", subtitles="x.srt", subtitle_size=True),
    dict(file="x.mp4", subtitles="x.srt", subtitle_size=129)])
def test_invalid_caption_options_fail_before_resources(options):
    with pytest.raises(ValueError):
        validate_subtitle_options(SimpleNamespace(**options))


def test_invalid_srt_fails_before_session_publication_or_directory(monkeypatch, tmp_path):
    path = captions(tmp_path, "invalid")
    args = SimpleNamespace(file="unused.mp4", subtitles=path, output=str(tmp_path / "session"))
    monkeypatch.setattr(session, "FramePublisher", lambda **kw: pytest.fail("Invalid SRT allocated publisher"))
    with pytest.raises(ValueError):
        session.serve(args)
    assert not (tmp_path / "session").exists()


def test_caption_layout_failure_keeps_original_video_and_visible_error(tmp_path):
    caption = FileSubtitleOverlay(captions(tmp_path, "1\n00:00:00,000 --> 00:00:01,000\n" + "큰 자막 " * 100), FONT, 32)
    image = np.full((90, 320, 4), 123, np.uint8)
    frame = StereoFrame(image, 1, 2, "3d", False, (0, 0, 160, 90))
    result = caption.composite(frame, 0)
    assert result is frame and np.all(image == 123)
    assert not caption.snapshot()["enabled"] and caption.snapshot()["error"]
    assert caption.composite(frame, 500_000_000) is frame


@pytest.mark.parametrize("mode", ["2d", "3d"])
def test_common_clock_uses_selected_vfr_rgb_pts_and_flat_postwarp_pixels(monkeypatch, tmp_path, mode):
    args, seen = setup(monkeypatch, tmp_path, Fast, mode=mode, seconds=.9)
    args.subtitles, args.subtitle_font, args.subtitle_size = captions(tmp_path), FONT, 8
    result = file_session.serve_file(args)
    assert result["error"] is None and result["subtitles"]["error"] is None
    rows = [json.loads(line) for line in (tmp_path / "session/presentation.jsonl").read_text().splitlines()]
    assert {row["pts_ns"] for row in rows} >= {0, 100_000_000, 350_000_000, 700_000_000}
    for (pixels, metadata), row in zip(seen, rows, strict=True):
        diag = row["subtitles"]["last"]
        assert diag["pts_ns"] == row["pts_ns"]
        assert diag["cue_indices"] == ([1] if row["pts_ns"] < 200_000_000 else [2] if row["pts_ns"] < 600_000_000 else [])
        if rect := diag["overlay_rect"]:
            x, y, w, h = rect
            np.testing.assert_array_equal(pixels[y:y+h, x:x+w], pixels[y:y+h, 160+x:160+x+w])
            assert np.any(pixels[y:y+h, x:x+w, 0] > 0)
        assert metadata["generation"] == row["generation"]


def test_existing_file_session_pause_holds_source_pts_caption_and_cached_frame_is_clean(monkeypatch, tmp_path, video):
    seen, originals = [], []
    class Publisher:
        skipped, epoch = 0, 10
        def publish(self, bgra, **meta):
            seen.append(bgra.copy())
            return True
        def close(self):
            pass
    from quest3d.stereo import StereoSynthesizer
    class Synth(StereoSynthesizer):
        def original_2d(self, *a, **kw):
            output = super().original_2d(*a, **kw)
            originals.append((output, output.bgra.copy()))
            return output
    worker = session.LatestAIWorker
    monkeypatch.setattr(session, "LatestAIWorker", lambda **kw: worker(**kw, engine_factory=Fast))
    monkeypatch.setattr(session, "StereoSynthesizer", Synth)
    monkeypatch.setattr(session, "FramePublisher", Publisher)
    monkeypatch.setattr(session, "ARTIFACT_DIR", tmp_path / "routing")
    args = SimpleNamespace(file=str(video), paused=True, file_playout_ms=120, output=str(tmp_path / "session"),
        mode="2d", disparity=2, cpu_threads=1, eye_width=320, eye_height=180, ai_size=280,
        seconds=.4, fps=30, max_frame_age_ms=200, subtitles=captions(tmp_path), subtitle_font=FONT, subtitle_size=16)
    result = session.serve(args)
    assert result["error"] is None and result["subtitles"]["error"] is None
    rows = [json.loads(line) for line in (tmp_path / "session/presentation.jsonl").read_text().splitlines()]
    assert len(rows) >= 4 and all(row["subtitles"]["last"]["cue_indices"] == [1] for row in rows)
    assert all(row["pts_ns"] == row["subtitles"]["last"]["pts_ns"] == 0 for row in rows)
    for output, clean in originals:
        np.testing.assert_array_equal(output.bgra, clean)
    assert originals and not np.array_equal(seen[-1], originals[-1][1])
