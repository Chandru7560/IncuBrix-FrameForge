from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from scenepilot.services.video_renderer import (
    BoundingBox,
    FFmpegRenderer,
    RenderingError,
    _file_sha256,
)


def scene_plan(asset_keys: dict[str, str] | None = None) -> dict:
    asset_keys = asset_keys or {
        "16:9": "source.mp4",
        "9:16": "source.mp4",
        "1:1": "source.mp4",
    }
    scene = {
        "scene_id": "scene_001",
        "narration_text": "A moving test pattern demonstrates video output.",
        "start_seconds": 0.0,
        "end_seconds": 2.0,
        "duration_seconds": 2.0,
        "intent_graph": {
            "central_concept": "test pattern",
            "intent": "Show a moving test pattern.",
        },
        "visual_retrieval": {
            "by_aspect_ratio": {
                ratio: {
                    "status": "selected",
                    "rights_passport": {
                        "asset_id": f"test-{ratio}",
                        "provider": "test-fixture",
                        "downloaded_asset_key": key,
                        "commercial_use_verification_status": "verified",
                        "licence_name": "CC0 1.0 Test Fixture",
                        "licence_evidence_url": "https://creativecommons.org/publicdomain/zero/1.0/",
                        "watermarked": False,
                        "watermark_verification": "clear",
                        "ai_generated": False,
                        "relevance_score": 0.98,
                        "selection_rationale": ["Generated test pattern source."],
                    },
                }
                for ratio, key in asset_keys.items()
            }
        },
    }
    return {
        "duration_seconds": 2.0,
        "coverage": {"coverage_ratio": 1.0, "boundary_tolerance_seconds": 0.05},
        "transcript": {
            "original_text": scene["narration_text"],
            "segments": [],
        },
        "scenes": [scene],
    }


def test_timeline_validation_rejects_missing_rights_verified_asset(settings):
    plan = scene_plan()
    del plan["scenes"][0]["visual_retrieval"]["by_aspect_ratio"]["16:9"]
    renderer = FFmpegRenderer(settings)
    with pytest.raises(RenderingError) as error:
        renderer._validate_plan(plan, ["16:9"])
    assert error.value.code == "SCENE_ASSET_MISSING"


def test_timeline_validation_rejects_duplicate_clips(settings):
    plan = scene_plan()
    second = json.loads(json.dumps(plan["scenes"][0]))
    second["scene_id"] = "scene_002"
    second["start_seconds"] = 1.0
    second["end_seconds"] = 2.0
    second["duration_seconds"] = 1.0
    plan["scenes"][0]["end_seconds"] = 1.0
    plan["scenes"][0]["duration_seconds"] = 1.0
    plan["scenes"].append(second)
    renderer = FFmpegRenderer(settings)
    with pytest.raises(RenderingError) as error:
        renderer._validate_plan(plan, ["16:9"])
    assert error.value.code == "DUPLICATE_SCENE_ASSET"


def test_asset_resolution_requires_clear_watermark_status_and_rights(settings, tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    test_settings = replace(settings, footage_directory=tmp_path)
    renderer = FFmpegRenderer(test_settings)
    plan = scene_plan()
    passport = plan["scenes"][0]["visual_retrieval"]["by_aspect_ratio"]["16:9"]["rights_passport"]
    passport["watermark_verification"] = "unverified"
    with pytest.raises(RenderingError) as error:
        renderer._asset_path(plan["scenes"][0], "16:9")
    assert error.value.code == "SCENE_ASSET_RIGHTS_REJECTED"


def test_captions_have_exact_scene_start_and_end_times(settings):
    renderer = FFmpegRenderer(settings)
    text = renderer._captions_srt(
        [
            {
                "start_seconds": 0.0,
                "end_seconds": 2.035,
                "narration_text": "A caption aligned to this scene.",
            }
        ]
    )
    assert "00:00:00,000 --> 00:00:02,035" in text
    assert "A caption aligned to this scene." in text


def test_center_crop_is_deterministic_and_subject_center_is_used(settings, tmp_path):
    class Detector:
        name = "test-detector"
        version = "test-1"

        def detect(self, video_path):
            return BoundingBox(center_x=0.75, center_y=0.5, width=0.2, height=0.4)

    renderer = FFmpegRenderer(settings)
    center = renderer._crop_position(
        tmp_path / "unused",
        width=1080,
        height=1920,
        source_width=1920,
        source_height=1080,
    )
    subject_renderer = FFmpegRenderer(settings, subject_detector=Detector())
    subject = subject_renderer._crop_position(
        tmp_path / "unused",
        width=1080,
        height=1920,
        source_width=1920,
        source_height=1080,
    )
    assert center["reframing"] == "deterministic_center_crop"
    assert subject["reframing"] == "test-detector:test-1"
    assert subject["x"] > center["x"]


def test_content_hash_changes_when_source_changes(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"first source bytes")
    before = _file_sha256(source)
    source.write_bytes(b"replacement source bytes")
    assert _file_sha256(source) != before


def test_interrupted_ffmpeg_process_is_reported_as_timeout(settings, monkeypatch):
    renderer = FFmpegRenderer(settings)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout", 1))

    monkeypatch.setattr("scenepilot.services.video_renderer.subprocess.run", timeout)
    with pytest.raises(RenderingError) as error:
        renderer._run(
            ["ffmpeg", "-i", "in.mp4", "out.mp4"],
            timeout=1,
            code="VIDEO_ASSEMBLY_FAILED",
        )
    assert error.value.code == "MEDIA_PROCESS_TIMEOUT"


def test_real_ffmpeg_render_outputs_mp4_and_reuses_unchanged_scene_cache(
    settings, tmp_path
):
    ffmpeg = shutil.which(settings.ffmpeg_binary)
    ffprobe = shutil.which(settings.ffprobe_binary)
    if not ffmpeg or not ffprobe:
        pytest.skip("Real render integration requires FFmpeg and FFprobe executables.")
    test_settings = replace(
        settings,
        ffmpeg_binary=ffmpeg,
        ffprobe_binary=ffprobe,
        footage_directory=tmp_path / "footage",
        output_directory=tmp_path / "outputs",
        render_cache_directory=tmp_path / "cache",
    )
    test_settings.footage_directory.mkdir(parents=True)
    source = test_settings.footage_directory / "source.mp4"
    audio = tmp_path / "narration.wav"
    subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30:duration=2",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
        timeout=60,
    )
    subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2",
            "-c:a",
            "pcm_s16le",
            str(audio),
        ],
        check=True,
        timeout=60,
    )
    renderer = FFmpegRenderer(test_settings)
    plan = scene_plan()
    first = renderer.render(
        job_id="render-cache-test",
        narration_path=audio,
        scene_plan=plan,
        aspect_ratios=["16:9", "9:16", "1:1"],
    )
    second = renderer.render(
        job_id="render-cache-test",
        narration_path=audio,
        scene_plan=plan,
        aspect_ratios=["16:9", "9:16", "1:1"],
    )
    expected_dimensions = {
        "16:9": (1920, 1080),
        "9:16": (1080, 1920),
        "1:1": (1080, 1080),
    }
    for output in second.manifest["outputs"]:
        video = first.output_directory / output["file"]
        assert video.is_file()
        assert video.stat().st_size > 10_000
        assert (output["width"], output["height"]) == expected_dimensions[
            output["aspect_ratio"]
        ]
        assert output["video_codec"] == "h264"
        assert output["audio_codec"] == "aac"
        assert output["frame_rate"] == 30
    assert (first.output_directory / "captions.srt").is_file()
    assert (first.output_directory / "timeline.json").is_file()
    assert (first.output_directory / "manifest.json").is_file()
    assert first.manifest["processing_statistics"]["scene_segments_rendered"] == 3
    assert second.manifest["processing_statistics"]["scene_segments_cached"] == 3
    metrics = second.manifest["production_metrics"]
    assert metrics["render_elapsed_seconds"]["value"] > 0
    assert metrics["timeline_coverage_ratio"]["value"] == 1.0
    assert metrics["repeat_clip_rate"]["value"] == 0
    assert metrics["cold_turnaround_seconds"]["status"] == "not_measured"
    assert metrics["peak_memory_bytes"]["status"] == "not_measured"
    continuity = second.manifest["processing_statistics"]["visual_continuity"]["16:9"]
    assert continuity["method"] == "FFmpeg signalstats at 1 fps on normalized scene segments"
    assert len(continuity["scenes"]) == 1
    assert continuity["scenes"][0]["sample_count"] > 0
