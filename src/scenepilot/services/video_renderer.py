"""FFmpeg rendering, content-addressed scene cache, and output validation."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Protocol

from scenepilot.config import Settings

RENDERER_VERSION = "scenepilot-ffmpeg-renderer-v1"
OUTPUT_DIMENSIONS = {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080)}


class RenderingError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        warnings: list[str] | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.warnings = warnings or []
        self.details = details or {}


@dataclass(frozen=True)
class BoundingBox:
    center_x: float
    center_y: float
    width: float
    height: float


class SubjectDetector(Protocol):
    name: str
    version: str

    def detect(self, video_path: Path) -> BoundingBox | None: ...


@dataclass(frozen=True)
class LicensedBackgroundMusic:
    path: Path
    provider: str
    licence_name: str
    licence_evidence_url: str
    attribution_required: bool
    attribution_text: str
    commercial_use_status: str
    watermarked: bool = False
    ai_generated: bool = False


@dataclass(frozen=True)
class RenderResult:
    artifact_names: tuple[str, ...]
    output_directory: Path
    manifest: dict[str, object]


class FFmpegRenderer:
    def __init__(
        self,
        settings: Settings,
        *,
        subject_detector: SubjectDetector | None = None,
    ) -> None:
        self.settings = settings
        self.subject_detector = subject_detector
        self.ffmpeg = settings.ffmpeg_binary
        self.ffprobe = settings.ffprobe_binary
        self.output_root = Path(settings.output_directory or "./data/outputs")
        self.cache_root = Path(
            settings.render_cache_directory or "./data/render-cache"
        ) / "segments"
        self._version_cache: dict[str, str] = {}

    def render(
        self,
        *,
        job_id: str,
        narration_path: Path,
        scene_plan: dict[str, object],
        aspect_ratios: list[str],
        captions: dict[str, object] | None = None,
        warnings: list[str] | None = None,
        providers: dict[str, object] | None = None,
        background_music: LicensedBackgroundMusic | None = None,
        seed: int | None = None,
    ) -> RenderResult:
        started = time.monotonic()
        warnings = list(warnings or [])
        self._validate_job_id(job_id)
        self._validate_plan(scene_plan, aspect_ratios)
        if not narration_path.is_file():
            raise RenderingError("NARRATION_FILE_MISSING", "Narration audio is not available.")
        narration_probe = self._probe_media(narration_path)
        self._first_stream(narration_probe, "audio")
        duration = float(scene_plan["duration_seconds"])
        tolerance = self.settings.render_duration_tolerance_seconds
        if abs(float(narration_probe["duration_seconds"]) - duration) > tolerance:
            raise RenderingError(
                "NARRATION_DURATION_MISMATCH",
                "The narration file duration does not match the planned timeline.",
                details={
                    "narration_duration_seconds": narration_probe["duration_seconds"],
                    "planned_duration_seconds": duration,
                    "tolerance_seconds": tolerance,
                },
            )
        self._validate_music(background_music)

        output_directory = self.output_root / job_id
        output_directory.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".render-", dir=output_directory))
        cache_hits = 0
        cache_misses = 0
        output_checks: dict[str, list[dict[str, object]]] = {}
        visual_continuity: dict[str, dict[str, object]] = {}
        artifact_names = ["captions.srt", "timeline.json", "manifest.json"]
        render_warnings = list(warnings)
        try:
            caption_file = staging / "captions.srt"
            caption_file.write_text(
                self._captions_srt(scene_plan["scenes"]), encoding="utf-8"
            )
            duplicate_checks = self._duplicate_asset_checks(scene_plan["scenes"])
            rendered_outputs = []
            for ratio in aspect_ratios:
                width, height = OUTPUT_DIMENSIONS[ratio]
                segments = []
                segment_profiles = []
                for scene in scene_plan["scenes"]:
                    if not isinstance(scene, dict):
                        raise RenderingError("INVALID_SCENE_PLAN", "A scene record is invalid.")
                    asset_path = self._asset_path(scene, ratio)
                    asset_probe = self._probe_media(asset_path)
                    segment_path, cache_hit, fallback_note = self._render_scene_segment(
                        scene=scene,
                        asset_path=asset_path,
                        asset_probe=asset_probe,
                        width=width,
                        height=height,
                        ratio=ratio,
                    )
                    segments.append(segment_path)
                    segment_profiles.append(self._measure_visual_profile(segment_path))
                    cache_hits += int(cache_hit)
                    cache_misses += int(not cache_hit)
                    if fallback_note:
                        render_warnings.append(
                            f"{scene.get('scene_id')}: {fallback_note}"
                        )

                output_name = f"video_{ratio.replace(':', '_')}.mp4"
                staged_video = staging / output_name
                concat_file = staging / f"concat_{ratio.replace(':', '_')}.txt"
                concat_file.write_text(
                    "".join(
                        f"file '{self._concat_path(segment)}'\n" for segment in segments
                    ),
                    encoding="utf-8",
                )
                burn_in = bool((captions or {}).get("burn_in", True))
                self._assemble_video(
                    narration_path=narration_path,
                    concat_file=concat_file,
                    output_path=staged_video,
                    duration=duration,
                    caption_file=caption_file if burn_in else None,
                    background_music=background_music,
                    staging_directory=staging,
                )
                checks, probe = self._validate_output(
                    staged_video,
                    ratio=ratio,
                    duration=duration,
                    tolerance=tolerance,
                    scene_plan=scene_plan,
                )
                output_checks[ratio] = checks
                visual_continuity[ratio] = self._continuity_report(
                    scene_plan["scenes"], segment_profiles
                )
                if any(check["status"] == "failed" for check in checks):
                    raise RenderingError(
                        "OUTPUT_VALIDATION_FAILED",
                        f"Rendered {ratio} output failed media validation.",
                        details={"aspect_ratio": ratio, "checks": checks},
                    )
                if any(check["status"] == "unavailable" for check in checks):
                    render_warnings.append(
                        f"Some {ratio} output quality checks could not be measured; see manifest."
                    )
                rendered_outputs.append(
                    {
                        "aspect_ratio": ratio,
                        "file": output_name,
                        "duration_seconds": probe["duration_seconds"],
                        "width": probe["width"],
                        "height": probe["height"],
                        "video_codec": probe["video_codec"],
                        "audio_codec": probe["audio_codec"],
                        "frame_rate": probe["frame_rate"],
                        "size_bytes": staged_video.stat().st_size,
                    }
                )
                artifact_names.append(output_name)

            timeline = self._timeline_record(scene_plan)
            (staging / "timeline.json").write_text(
                json.dumps(timeline, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            manifest = {
                "schema_version": "1.0",
                "renderer": {
                    "name": RENDERER_VERSION,
                    "ffmpeg": self._tool_version(self.ffmpeg),
                    "ffprobe": self._tool_version(self.ffprobe),
                    "subject_detector": (
                        {
                            "name": self.subject_detector.name,
                            "version": self.subject_detector.version,
                        }
                        if self.subject_detector
                        else None
                    ),
                },
                "providers": providers or {},
                "configuration": {
                    "aspect_ratios": aspect_ratios,
                    "output_dimensions": {
                        ratio: OUTPUT_DIMENSIONS[ratio] for ratio in aspect_ratios
                    },
                    "fps": 30,
                    "video_codec": "h264",
                    "audio_codec": "aac",
                    "pixel_format": "yuv420p",
                    "audio_sample_rate": 48000,
                    "colour_normalization": "FFmpeg normalize filter per scene",
                    "slow_motion_fallback_max_factor": self.settings.render_max_slowdown,
                    "captions_burned_in": bool((captions or {}).get("burn_in", True)),
                    "cache_version": RENDERER_VERSION,
                },
                "seed": seed,
                "narration_duration_seconds": duration,
                "narration_probe": narration_probe,
                "outputs": rendered_outputs,
                "rights": {
                    "scene_assets": [
                        {
                            "scene_id": scene["scene_id"],
                            "aspect_ratio": ratio,
                            **scene["visual_retrieval"]["by_aspect_ratio"][ratio][
                                "rights_passport"
                            ],
                        }
                        for ratio in aspect_ratios
                        for scene in scene_plan["scenes"]
                    ],
                    "background_music": self._music_passport(background_music),
                },
                "costs": {
                    "estimated": None,
                    "actual_known": None,
                    "currency": None,
                    "note": "Provider and infrastructure costs are unavailable unless supplied by integrations.",
                },
                "processing_statistics": {
                    "scene_count": len(scene_plan["scenes"]),
                    "scene_segments_cached": cache_hits,
                    "scene_segments_rendered": cache_misses,
                    "cache_hit_ratio": (
                        cache_hits / (cache_hits + cache_misses)
                        if cache_hits + cache_misses
                        else 0.0
                    ),
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "duplicate_asset_checks": duplicate_checks,
                    "output_checks": output_checks,
                    "visual_continuity": visual_continuity,
                },
                "production_metrics": {
                    "cold_turnaround_seconds": {
                        "value": None,
                        "status": "not_measured",
                        "reason": "A controlled cold-cache benchmark is required.",
                    },
                    "warm_turnaround_seconds": {
                        "value": None,
                        "status": "not_measured",
                        "reason": "A controlled warm-cache benchmark is required.",
                    },
                    "render_elapsed_seconds": {
                        "value": round(time.monotonic() - started, 3),
                        "status": "measured",
                        "scope": "This render invocation only; excludes queue wait and input preparation.",
                    },
                    "cost_per_finished_video_minute": {
                        "value": None,
                        "status": "unavailable",
                        "reason": "Provider pricing and infrastructure allocation are not configured.",
                    },
                    "timeline_coverage_ratio": {
                        "value": scene_plan.get("coverage", {}).get("coverage_ratio"),
                        "status": "measured_from_validated_scene_plan",
                    },
                    "independent_relevance": [
                        {
                            "scene_id": scene.get("scene_id"),
                            "aspect_ratio": ratio,
                            **selection["independent_relevance_evaluation"],
                        }
                        for ratio in aspect_ratios
                        for scene in scene_plan["scenes"]
                        for selection in [
                            scene["visual_retrieval"]["by_aspect_ratio"][ratio]
                        ]
                        if isinstance(
                            selection.get("independent_relevance_evaluation"), dict
                        )
                    ],
                    "repeat_clip_rate": {
                        "value": len(duplicate_checks)
                        / max(1, len(scene_plan["scenes"]) * len(aspect_ratios)),
                        "status": "measured",
                        "duplicate_asset_count": len(duplicate_checks),
                    },
                    "peak_memory_bytes": {
                        "value": None,
                        "status": "not_measured",
                        "reason": "Worker and FFmpeg process peak RSS are not instrumented.",
                    },
                    "max_concurrent_jobs": {
                        "value": None,
                        "status": "not_measured",
                        "reason": "Requires a controlled multi-job load test.",
                    },
                },
                "warnings": list(dict.fromkeys(render_warnings)),
            }
            (staging / "manifest.json").write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            for name in artifact_names:
                os.replace(staging / name, output_directory / name)
            return RenderResult(tuple(artifact_names), output_directory, manifest)
        except RenderingError:
            raise
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise RenderingError(
                "RENDERING_FAILED",
                "The requested video could not be rendered.",
                details={"failure_type": type(exc).__name__},
            ) from exc
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _render_scene_segment(
        self,
        *,
        scene: dict[str, object],
        asset_path: Path,
        asset_probe: dict[str, object],
        width: int,
        height: int,
        ratio: str,
    ) -> tuple[Path, bool, str | None]:
        scene_duration = float(scene["duration_seconds"])
        asset_duration = float(asset_probe["duration_seconds"])
        slowdown = scene_duration / asset_duration
        fallback_note = None
        if slowdown > 1.0005:
            if slowdown > self.settings.render_max_slowdown:
                raise RenderingError(
                    "SCENE_FOOTAGE_TOO_SHORT",
                    "No eligible clip can cover this scene without excessive slow motion.",
                    details={
                        "scene_id": scene.get("scene_id"),
                        "scene_duration_seconds": scene_duration,
                        "asset_duration_seconds": asset_duration,
                        "maximum_slowdown_factor": self.settings.render_max_slowdown,
                    },
                )
            fallback_note = (
                f"Short source clip was extended {slowdown:.3f}x with motion-compensated "
                "slow motion; no frozen-frame or loop fallback was used."
            )
        source_video = self._first_stream(asset_probe, "video")
        digest = _file_sha256(asset_path)
        crop_position = self._crop_position(
            asset_path,
            width=width,
            height=height,
            source_width=int(source_video.get("width", 0)),
            source_height=int(source_video.get("height", 0)),
        )
        key_material = {
            "renderer_version": RENDERER_VERSION,
            "asset_sha256": digest,
            "scene_duration_seconds": scene_duration,
            "ratio": ratio,
            "output_dimensions": [width, height],
            "source_crop": crop_position,
            "slowdown_factor": round(slowdown, 6),
            "ffmpeg": self._tool_version(self.ffmpeg),
        }
        cache_key = hashlib.sha256(
            json.dumps(key_material, sort_keys=True).encode("utf-8")
        ).hexdigest()
        self.cache_root.mkdir(parents=True, exist_ok=True)
        cached = self.cache_root / f"{cache_key}.mp4"
        if cached.is_file():
            try:
                self._probe_video_segment(
                    cached, width, height, expected_duration=scene_duration
                )
                return cached, True, fallback_note
            except RenderingError:
                cached.unlink(missing_ok=True)

        temp_segment = self.cache_root / f".{cache_key}-{uuid.uuid4().hex}.mp4"
        filters = [
            "settb=AVTB",
            f"setpts={slowdown:.8f}*(PTS-STARTPTS)" if slowdown > 1.0005 else "setpts=PTS-STARTPTS",
        ]
        if slowdown > 1.0005:
            filters.append("minterpolate=fps=30:mi_mode=mci")
        filters.extend(
            [
                f"trim=duration={scene_duration:.6f}",
                "setpts=PTS-STARTPTS",
                "fps=30",
                f"scale={crop_position['scaled_width']}:{crop_position['scaled_height']}",
                f"crop={width}:{height}:{crop_position['x']}:{crop_position['y']}",
                "setsar=1",
                "normalize=blackpt=black:whitept=white:smoothing=50",
                "format=yuv420p",
                "tpad=stop_mode=clone:stop_duration=0.1",
                f"trim=duration={scene_duration:.6f}",
                "setpts=PTS-STARTPTS",
            ]
        )
        try:
            self._run(
                [
                    self.ffmpeg,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(asset_path),
                    "-vf",
                    ",".join(filters),
                    "-an",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "medium",
                    "-crf",
                    "20",
                    "-pix_fmt",
                    "yuv420p",
                    "-r",
                    "30",
                    "-fps_mode",
                    "cfr",
                    "-video_track_timescale",
                    "30000",
                    "-movflags",
                    "+faststart",
                    str(temp_segment),
                ],
                timeout=self.settings.render_timeout_seconds,
                code="SCENE_SEGMENT_RENDER_FAILED",
            )
            self._probe_video_segment(
                temp_segment, width, height, expected_duration=scene_duration
            )
            os.replace(temp_segment, cached)
        except RenderingError:
            temp_segment.unlink(missing_ok=True)
            raise
        except OSError as exc:
            temp_segment.unlink(missing_ok=True)
            raise RenderingError(
                "SCENE_CACHE_WRITE_FAILED", "Rendered scene segment could not be cached."
            ) from exc
        return cached, False, fallback_note

    def _assemble_video(
        self,
        *,
        narration_path: Path,
        concat_file: Path,
        output_path: Path,
        duration: float,
        caption_file: Path | None,
        background_music: LicensedBackgroundMusic | None,
        staging_directory: Path,
    ) -> None:
        command = [
            self.ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-i",
            str(narration_path),
        ]
        if background_music is not None:
            command.extend(["-stream_loop", "-1", "-i", str(background_music.path)])
        audio_input_index = 1
        narration_filter = (
            f"[{audio_input_index}:a]aresample=48000:async=1:first_pts=0,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"apad=whole_dur={duration:.6f},atrim=duration={duration:.6f},"
            "asetpts=PTS-STARTPTS[narr]"
        )
        if background_music is None:
            audio_graph = narration_filter + ";[narr]alimiter=limit=0.95[aout]"
        else:
            music_index = 2
            audio_graph = (
                narration_filter
                + f";[{music_index}:a]aresample=48000:async=1:first_pts=0,"
                "aformat=sample_fmts=fltp:channel_layouts=stereo,volume=0.16[bg]"
                ";[bg][narr]sidechaincompress=threshold=0.03:ratio=8:"
                "attack=20:release=250[ducked]"
                ";[narr][ducked]amix=inputs=2:duration=first:normalize=0,"
                "alimiter=limit=0.95[aout]"
            )
        video_filters = [
            "fps=30",
            "tpad=stop_mode=clone:stop_duration=0.1",
            f"trim=duration={duration:.6f}",
            "setpts=PTS-STARTPTS",
        ]
        if caption_file is not None:
            video_filters.append("subtitles=captions.srt")
        video_graph = "[0:v]" + ",".join(video_filters) + "[vout]"
        command.extend(
            [
                "-filter_complex",
                video_graph + ";" + audio_graph,
                "-map",
                "[vout]",
                "-map",
                "[aout]",
                "-t",
                f"{duration:.6f}",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-r",
                "30",
                "-fps_mode",
                "cfr",
                "-video_track_timescale",
                "30000",
                "-color_primaries",
                "bt709",
                "-color_trc",
                "bt709",
                "-colorspace",
                "bt709",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-ar",
                "48000",
                "-movflags",
                "+faststart",
                str(output_path),
            ]
        )
        self._run(
            command,
            timeout=self.settings.render_timeout_seconds,
            code="VIDEO_ASSEMBLY_FAILED",
            cwd=staging_directory,
        )

    def _validate_output(
        self,
        path: Path,
        *,
        ratio: str,
        duration: float,
        tolerance: float,
        scene_plan: dict[str, object],
    ) -> tuple[list[dict[str, object]], dict[str, object]]:
        probe = self._probe_media(path)
        width, height = OUTPUT_DIMENSIONS[ratio]
        video = self._first_stream(probe, "video")
        audio = self._first_stream(probe, "audio")
        frame_rate = _fraction(video.get("avg_frame_rate") or video.get("r_frame_rate"))
        checks = [
            _check("file_integrity", path.is_file() and path.stat().st_size > 0),
            _check("video_codec_h264", video.get("codec_name") == "h264"),
            _check("audio_codec_aac", audio.get("codec_name") == "aac"),
            _check("dimensions", int(video.get("width", 0)) == width and int(video.get("height", 0)) == height),
            _check("frame_rate_30_fps", frame_rate is not None and abs(frame_rate - 30) < 0.01, measured=frame_rate),
            _check("pixel_format_yuv420p", video.get("pix_fmt") == "yuv420p"),
            _check("audio_presence", bool(audio)),
            _check("audio_sample_rate_48khz", audio.get("sample_rate") == "48000"),
            _check(
                "output_duration",
                abs(float(probe["duration_seconds"]) - duration) <= tolerance,
                measured=probe["duration_seconds"],
                expected=duration,
                tolerance=tolerance,
            ),
            _check("timeline_coverage", self._timeline_covers(scene_plan)),
            _check("duplicate_clip_check", not self._duplicate_asset_checks(scene_plan["scenes"])),
        ]
        decode_check = self._decode_integrity(path)
        checks.append(decode_check)
        checks.extend(self._visual_quality_checks(path))
        return checks, {
            "duration_seconds": probe["duration_seconds"],
            "width": int(video.get("width", 0)),
            "height": int(video.get("height", 0)),
            "video_codec": video.get("codec_name"),
            "audio_codec": audio.get("codec_name"),
            "frame_rate": frame_rate,
        }

    def _visual_quality_checks(self, path: Path) -> list[dict[str, object]]:
        checks = []
        for name, filter_expression, pattern in (
            ("black_frame_check", "blackdetect=d=0.10:pix_th=0.10", r"black_start:"),
            (
                "frozen_frame_check",
                "freezedetect=n=-50dB:d=3",
                r"freeze_duration:([0-9.]+)",
            ),
        ):
            try:
                result = self._run(
                    [
                        self.ffmpeg,
                        "-nostdin",
                        "-hide_banner",
                        "-i",
                        str(path),
                        "-vf",
                        filter_expression,
                        "-an",
                        "-f",
                        "null",
                        "-",
                    ],
                    timeout=self.settings.render_timeout_seconds,
                    code="QUALITY_CHECK_UNAVAILABLE",
                    check=False,
                )
            except RenderingError:
                checks.append({"name": name, "status": "unavailable", "measured": None})
                continue
            diagnostic = result.stdout.decode("utf-8", "replace") + result.stderr.decode(
                "utf-8", "replace"
            )
            matches = re.findall(pattern, diagnostic)
            if result.returncode != 0:
                checks.append({"name": name, "status": "unavailable", "measured": None})
            elif name == "frozen_frame_check":
                freezes = [float(match) for match in matches]
                checks.append(
                    {
                        "name": name,
                        "status": "failed" if any(value > 3.0 for value in freezes) else "passed",
                        "measured": freezes,
                        "threshold_seconds": 3.0,
                    }
                )
            else:
                checks.append(
                    {
                        "name": name,
                        "status": "failed" if matches else "passed",
                        "measured": len(matches),
                    }
                )
        return checks

    def _measure_visual_profile(self, path: Path) -> dict[str, object]:
        try:
            result = self._run(
                [
                    self.ffmpeg,
                    "-nostdin",
                    "-hide_banner",
                    "-i",
                    str(path),
                    "-vf",
                    "fps=1,signalstats,metadata=print:file=-",
                    "-an",
                    "-f",
                    "null",
                    "-",
                ],
                timeout=self.settings.render_timeout_seconds,
                code="VISUAL_CONTINUITY_UNAVAILABLE",
                check=False,
            )
        except RenderingError:
            return {"status": "unavailable", "samples": []}
        diagnostic = result.stdout.decode("utf-8", "replace") + result.stderr.decode(
            "utf-8", "replace"
        )
        if result.returncode != 0:
            return {"status": "unavailable", "samples": []}
        keys = ("YAVG", "UAVG", "VAVG", "SATAVG")
        values = {
            key: re.findall(
                rf"lavfi\.signalstats\.{key}=([0-9.+-]+)", diagnostic
            )
            for key in keys
        }
        sample_count = min((len(samples) for samples in values.values()), default=0)
        samples = [
            {
                key: float(values[key][index])
                for key in keys
            }
            for index in range(sample_count)
        ]
        if not samples:
            return {"status": "unavailable", "samples": []}
        return {"status": "measured", "samples": samples}

    @staticmethod
    def _continuity_report(
        scenes: list[dict[str, object]],
        profiles: list[dict[str, object]],
    ) -> dict[str, object]:
        records = []
        transitions = []
        for index, (scene, profile) in enumerate(zip(scenes, profiles, strict=True)):
            samples = profile.get("samples", [])
            selections = (
                scene.get("visual_retrieval", {}).get("by_aspect_ratio", {})
                if isinstance(scene.get("visual_retrieval"), dict)
                else {}
            )
            selected = next(
                (
                    selection
                    for selection in selections.values()
                    if isinstance(selection, dict) and selection.get("status") == "selected"
                ),
                {},
            )
            scene_duration = float(scene.get("duration_seconds") or 0)
            if samples:
                means = {
                    key: round(
                        sum(float(sample[key]) for sample in samples) / len(samples), 2
                    )
                    for key in ("YAVG", "UAVG", "VAVG", "SATAVG")
                }
            else:
                means = None
            records.append(
                {
                    "scene_id": scene.get("scene_id"),
                    "duration_seconds": scene.get("duration_seconds"),
                    "sample_count": len(samples),
                    "mean_signalstats": means,
                    "pacing_status": "failed" if scene_duration < 0.5 else "passed",
                    "shot_type": selected.get("selected_shot_type"),
                }
            )
            if index == 0:
                continue
            previous = records[index - 1]
            current = records[index]
            previous_stats = previous["mean_signalstats"]
            current_stats = current["mean_signalstats"]
            pacing = {
                "name": "minimum_scene_pacing",
                "status": current["pacing_status"],
                "measured_seconds": scene_duration,
                "minimum_seconds": 0.5,
            }
            if previous_stats is None or current_stats is None:
                transitions.append(
                    {
                        "from_scene": previous["scene_id"],
                        "to_scene": current["scene_id"],
                        "status": "unavailable",
                        "brightness_delta": None,
                        "chroma_delta": None,
                        "saturation_delta": None,
                        "pacing": pacing,
                    }
                )
                continue
            brightness_delta = round(abs(current_stats["YAVG"] - previous_stats["YAVG"]), 2)
            chroma_delta = round(
                math.hypot(
                    current_stats["UAVG"] - previous_stats["UAVG"],
                    current_stats["VAVG"] - previous_stats["VAVG"],
                ),
                2,
            )
            saturation_delta = round(
                abs(current_stats["SATAVG"] - previous_stats["SATAVG"]), 2
            )
            shot_type = current["shot_type"]
            repeated_shot = bool(shot_type and shot_type == previous["shot_type"])
            transitions.append(
                {
                    "from_scene": previous["scene_id"],
                    "to_scene": current["scene_id"],
                    "status": "warning"
                    if brightness_delta > 35 or chroma_delta > 45 or saturation_delta > 60
                    else "passed",
                    "brightness_delta": brightness_delta,
                    "brightness_threshold": 35,
                    "chroma_delta": chroma_delta,
                    "chroma_threshold": 45,
                    "saturation_delta": saturation_delta,
                    "saturation_threshold": 60,
                    "shot_type_repeated": repeated_shot,
                    "shot_diversity_status": (
                        "warning" if repeated_shot else "passed"
                    ),
                    "pacing": pacing,
                }
            )
        return {
            "method": "FFmpeg signalstats at 1 fps on normalized scene segments",
            "status": (
                "unavailable"
                if any(profile.get("status") != "measured" for profile in profiles)
                else "measured"
            ),
            "interpretation": (
                "Threshold exceedance flags an editorial review; these metrics do not prove perceptual continuity."
            ),
            "scenes": records,
            "transitions": transitions,
        }

    def _decode_integrity(self, path: Path) -> dict[str, object]:
        try:
            result = self._run(
                [
                    self.ffmpeg,
                    "-nostdin",
                    "-hide_banner",
                    "-v",
                    "error",
                    "-i",
                    str(path),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0",
                    "-f",
                    "null",
                    "-",
                ],
                timeout=self.settings.render_timeout_seconds,
                code="OUTPUT_DECODE_FAILED",
                check=False,
            )
        except RenderingError:
            return {"name": "full_decode_integrity", "status": "unavailable", "measured": None}
        return {
            "name": "full_decode_integrity",
            "status": "passed" if result.returncode == 0 else "failed",
            "measured": result.returncode == 0,
        }

    def _crop_position(
        self,
        path: Path,
        *,
        width: int,
        height: int,
        source_width: int,
        source_height: int,
    ) -> dict[str, int | str | None]:
        scale = max(width / source_width, height / source_height)
        scaled_width = math.ceil(source_width * scale / 2) * 2
        scaled_height = math.ceil(source_height * scale / 2) * 2
        box = self.subject_detector.detect(path) if self.subject_detector else None
        center_x = box.center_x if box else 0.5
        center_y = box.center_y if box else 0.5
        x = min(max(0, round(center_x * scaled_width - width / 2)), scaled_width - width)
        y = min(max(0, round(center_y * scaled_height - height / 2)), scaled_height - height)
        return {
            "scaled_width": scaled_width,
            "scaled_height": scaled_height,
            "x": x,
            "y": y,
            "reframing": (
                f"{self.subject_detector.name}:{self.subject_detector.version}"
                if box and self.subject_detector
                else "deterministic_center_crop"
            ),
        }

    def _asset_path(self, scene: dict[str, object], aspect_ratio: str) -> Path:
        retrieval = scene.get("visual_retrieval")
        if not isinstance(retrieval, dict):
            raise RenderingError(
                "SCENE_ASSET_MISSING", "A scene has no visual retrieval selection."
            )
        by_ratio = retrieval.get("by_aspect_ratio")
        selection = by_ratio.get(aspect_ratio) if isinstance(by_ratio, dict) else None
        if not isinstance(selection, dict) or selection.get("status") != "selected":
            raise RenderingError(
                "SCENE_ASSET_MISSING",
                "A scene has no rights-verified footage selection for the requested ratio.",
                details={"scene_id": scene.get("scene_id")},
            )
        passport = selection.get("rights_passport")
        if not isinstance(passport, dict):
            raise RenderingError("SCENE_ASSET_MISSING", "Selected footage passport is invalid.")
        key = str(passport.get("downloaded_asset_key") or "")
        if not key or Path(key).name != key or "\\" in key or "/" in key:
            raise RenderingError(
                "SCENE_ASSET_MISSING", "Selected footage storage key is invalid."
            )
        path = Path(self.settings.footage_directory or "./data/footage") / key
        if not path.is_file():
            raise RenderingError(
                "SCENE_ASSET_MISSING",
                "A selected footage asset is not present in configured storage.",
                details={"scene_id": scene.get("scene_id")},
            )
        if (
            passport.get("commercial_use_verification_status") != "verified"
            or not passport.get("licence_name")
            or not str(passport.get("licence_evidence_url") or "").startswith("https://")
            or passport.get("watermark_verification") != "clear"
            or passport.get("watermarked")
            or passport.get("ai_generated")
        ):
            raise RenderingError(
                "SCENE_ASSET_RIGHTS_REJECTED",
                "A scene asset did not pass the publishable rights policy.",
                details={"scene_id": scene.get("scene_id")},
            )
        return path

    def _probe_media(self, path: Path) -> dict[str, object]:
        result = self._run(
            [
                self.ffprobe,
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(path),
            ],
            timeout=min(self.settings.render_timeout_seconds, 60.0),
            code="MEDIA_PROBE_FAILED",
        )
        try:
            payload = json.loads(result.stdout)
            duration = float(payload["format"]["duration"])
            streams = payload["streams"]
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise RenderingError(
                "MEDIA_PROBE_INVALID",
                "Media probe returned incomplete or invalid metadata.",
            ) from exc
        if not math.isfinite(duration) or duration <= 0 or not isinstance(streams, list):
            raise RenderingError("MEDIA_PROBE_INVALID", "Media duration or streams are invalid.")
        return {
            "duration_seconds": duration,
            "streams": streams,
            "format_name": payload["format"].get("format_name"),
        }

    def _probe_video_segment(
        self,
        path: Path,
        width: int,
        height: int,
        *,
        expected_duration: float,
    ) -> None:
        probe = self._probe_media(path)
        video = self._first_stream(probe, "video")
        if (
            video.get("codec_name") != "h264"
            or int(video.get("width", 0)) != width
            or int(video.get("height", 0)) != height
            or _fraction(video.get("avg_frame_rate")) != 30
            or abs(float(probe["duration_seconds"]) - expected_duration)
            > self.settings.render_duration_tolerance_seconds
        ):
            raise RenderingError(
                "SCENE_CACHE_INVALID", "Cached scene segment does not match the render profile."
            )

    def _run(
        self,
        command: list[str],
        *,
        timeout: float,
        code: str,
        check: bool = True,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                check=False,
                timeout=timeout,
                cwd=cwd,
            )
        except FileNotFoundError as exc:
            raise RenderingError(
                "MEDIA_TOOL_UNAVAILABLE",
                "The configured FFmpeg/FFprobe executable is not installed.",
                details={"tool": Path(command[0]).name},
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RenderingError(
                "MEDIA_PROCESS_TIMEOUT",
                "Media processing exceeded its configured time limit.",
                warnings=[f"Timed-out operation: {code}"],
                details={"timeout_seconds": timeout},
            ) from exc
        if check and result.returncode != 0:
            raise RenderingError(
                code,
                "The FFmpeg/FFprobe operation failed.",
                details={"return_code": result.returncode},
            )
        return result

    def _tool_version(self, tool: str) -> str:
        if tool in self._version_cache:
            return self._version_cache[tool]
        result = self._run(
            [tool, "-version"],
            timeout=10,
            code="MEDIA_TOOL_VERSION_FAILED",
        )
        line = result.stdout.decode("utf-8", "replace").splitlines()
        version = line[0][:200] if line else "version_unavailable"
        self._version_cache[tool] = version
        return version

    def _validate_plan(self, scene_plan: dict[str, object], aspect_ratios: list[str]) -> None:
        if not aspect_ratios or any(ratio not in OUTPUT_DIMENSIONS for ratio in aspect_ratios):
            raise RenderingError("INVALID_ASPECT_RATIO", "An output aspect ratio is unsupported.")
        scenes = scene_plan.get("scenes")
        duration = scene_plan.get("duration_seconds")
        if not isinstance(scenes, list) or not scenes or not isinstance(duration, (int, float)):
            raise RenderingError("INVALID_SCENE_PLAN", "Scene plan is empty or has no duration.")
        if len(set(aspect_ratios)) != len(aspect_ratios):
            raise RenderingError("INVALID_ASPECT_RATIO", "Output aspect ratios must be unique.")
        tolerance = self.settings.narration_boundary_tolerance_seconds
        if not self._timeline_covers(scene_plan):
            raise RenderingError("TIMELINE_COVERAGE_FAILED", "Scene timeline has gaps or overlaps.")
        previous_end = 0.0
        asset_ids = set()
        for index, scene in enumerate(scenes):
            if not isinstance(scene, dict):
                raise RenderingError("INVALID_SCENE_PLAN", "A scene record is invalid.")
            start = float(scene.get("start_seconds", -1))
            end = float(scene.get("end_seconds", -1))
            if start < previous_end - tolerance or end <= start:
                raise RenderingError("TIMELINE_COVERAGE_FAILED", "Scene timings are not ordered.")
            if end - start < 0.5:
                raise RenderingError(
                    "SCENE_PACING_TOO_SHORT",
                    "A scene is shorter than the minimum 0.5-second pacing threshold.",
                    details={"scene_id": scene.get("scene_id"), "duration_seconds": end - start},
                )
            previous_end = end
            selected = scene.get("visual_retrieval", {}).get("by_aspect_ratio", {})
            for ratio in aspect_ratios:
                entry = selected.get(ratio) if isinstance(selected, dict) else None
                passport = entry.get("rights_passport") if isinstance(entry, dict) else None
                if not isinstance(passport, dict):
                    raise RenderingError(
                        "SCENE_ASSET_MISSING",
                        "Every scene and requested ratio requires a selected footage asset.",
                        details={"scene_id": scene.get("scene_id"), "aspect_ratio": ratio},
                    )
                key = f"{passport.get('provider')}:{passport.get('asset_id')}"
                ratio_asset_key = f"{ratio}:{key}"
                if ratio_asset_key in asset_ids:
                    raise RenderingError(
                        "DUPLICATE_SCENE_ASSET",
                        "A clip cannot be repeated within the same output.",
                        details={"scene_id": scene.get("scene_id"), "asset_id": key},
                    )
                asset_ids.add(ratio_asset_key)
            if index == 0 and start > tolerance:
                raise RenderingError("TIMELINE_COVERAGE_FAILED", "Timeline does not start at zero.")
        if abs(float(duration) - previous_end) > tolerance:
            raise RenderingError("TIMELINE_COVERAGE_FAILED", "Timeline does not cover narration duration.")

    def _timeline_covers(self, scene_plan: dict[str, object]) -> bool:
        try:
            duration = float(scene_plan["duration_seconds"])
            scenes = scene_plan["scenes"]
            tolerance = self.settings.narration_boundary_tolerance_seconds
            return (
                bool(scenes)
                and abs(float(scenes[0]["start_seconds"])) <= tolerance
                and abs(float(scenes[-1]["end_seconds"]) - duration) <= tolerance
                and all(
                    abs(float(current["start_seconds"]) - float(previous["end_seconds"]))
                    <= tolerance
                    for previous, current in zip(scenes, scenes[1:])
                )
            )
        except (KeyError, TypeError, ValueError):
            return False

    @staticmethod
    def _duplicate_asset_checks(scenes: list[dict[str, object]]) -> list[dict[str, object]]:
        duplicates = []
        for ratio in OUTPUT_DIMENSIONS:
            seen = set()
            for scene in scenes:
                retrieval = scene.get("visual_retrieval", {})
                selection = retrieval.get("by_aspect_ratio", {}).get(ratio, {})
                passport = selection.get("rights_passport")
                if not isinstance(passport, dict):
                    continue
                key = f"{passport.get('provider')}:{passport.get('asset_id')}"
                if key in seen:
                    duplicates.append({"aspect_ratio": ratio, "asset_id": key})
                seen.add(key)
        return duplicates

    def _validate_music(self, music: LicensedBackgroundMusic | None) -> None:
        if music is None:
            return
        if (
            not music.path.is_file()
            or music.commercial_use_status != "verified"
            or not music.licence_name.strip()
            or not music.licence_evidence_url.startswith("https://")
            or music.watermarked
            or music.ai_generated
        ):
            raise RenderingError(
                "BACKGROUND_MUSIC_RIGHTS_REJECTED",
                "Background music must have verified commercial rights and no watermark.",
            )
        probe = self._probe_media(music.path)
        if not self._first_stream(probe, "audio"):
            raise RenderingError(
                "BACKGROUND_MUSIC_INVALID", "Background music contains no audio stream."
            )

    @staticmethod
    def _music_passport(music: LicensedBackgroundMusic | None) -> dict[str, object] | None:
        if music is None:
            return None
        return {
            "provider": music.provider,
            "licence_name": music.licence_name,
            "licence_evidence_url": music.licence_evidence_url,
            "attribution_required": music.attribution_required,
            "attribution_text": music.attribution_text,
            "commercial_use_verification_status": music.commercial_use_status,
            "ai_generated": music.ai_generated,
            "watermarked": music.watermarked,
        }

    @staticmethod
    def _timeline_record(scene_plan: dict[str, object]) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "narration_duration_seconds": scene_plan["duration_seconds"],
            "coverage": scene_plan["coverage"],
            "transcript": scene_plan.get("transcript"),
            "scenes": [
                {
                    "scene_id": scene.get("scene_id"),
                    "narration_text": scene.get("narration_text"),
                    "start_seconds": scene.get("start_seconds"),
                    "end_seconds": scene.get("end_seconds"),
                    "clip": scene.get("visual_retrieval"),
                }
                for scene in scene_plan["scenes"]
            ],
        }

    @staticmethod
    def _captions_srt(scenes: list[dict[str, object]]) -> str:
        cues = []
        for index, scene in enumerate(scenes, start=1):
            text = str(scene.get("narration_text") or "").replace("\r", " ").replace("\n", " ")
            cues.extend(
                [
                    str(index),
                    f"{_srt_timestamp(float(scene['start_seconds']))} --> "
                    f"{_srt_timestamp(float(scene['end_seconds']))}",
                    text,
                    "",
                ]
            )
        return "\n".join(cues)

    @staticmethod
    def _concat_path(path: Path) -> str:
        normalized = str(path.resolve()).replace("\\", "/")
        if "'" in normalized:
            normalized = normalized.replace("'", "'\\''")
        return normalized

    @staticmethod
    def _first_stream(probe: dict[str, object], codec_type: str) -> dict[str, object]:
        streams = [
            stream
            for stream in probe.get("streams", [])
            if isinstance(stream, dict) and stream.get("codec_type") == codec_type
        ]
        if not streams:
            raise RenderingError(
                "OUTPUT_STREAM_MISSING",
                f"Media file contains no {codec_type} stream.",
                details={"stream_type": codec_type},
            )
        return streams[0]

    @staticmethod
    def _validate_job_id(job_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
            raise RenderingError("INVALID_JOB_ID", "Job identifier is not safe for output paths.")


def _check(
    name: str,
    passed: bool,
    *,
    measured: object = None,
    expected: object = None,
    tolerance: float | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "status": "passed" if passed else "failed",
        "measured": measured if measured is not None else passed,
        "expected": expected,
        "tolerance": tolerance,
    }


def _fraction(value: object) -> float | None:
    try:
        return float(Fraction(str(value)))
    except (ValueError, ZeroDivisionError, TypeError):
        return None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"
