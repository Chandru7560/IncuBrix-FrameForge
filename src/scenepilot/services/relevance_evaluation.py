"""Optional independent evaluation of narration against sampled selected-footage frames."""

from __future__ import annotations

import base64
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from scenepilot.config import Settings


class RelevanceEvaluationError(RuntimeError):
    """A configured independent evaluator could not produce a validated score."""


@dataclass(frozen=True)
class RelevanceEvaluation:
    provider: str
    model: str
    version: str
    score: float
    rationale: str
    sampled_timestamps_seconds: tuple[float, ...]
    normalization: str = "model_score_0_to_100_divided_by_100"


class VisionLanguageEvaluator(Protocol):
    provider: str
    model: str

    def evaluate(
        self,
        *,
        narration_text: str,
        intent: dict[str, object],
        selected_video: Path,
    ) -> RelevanceEvaluation: ...


class OpenAICompatibleVisionEvaluator:
    provider = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        ffmpeg_binary: str,
        ffprobe_binary: str,
        timeout_seconds: float,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.ffmpeg_binary = ffmpeg_binary
        self.ffprobe_binary = ffprobe_binary
        self.timeout_seconds = timeout_seconds

    def evaluate(
        self,
        *,
        narration_text: str,
        intent: dict[str, object],
        selected_video: Path,
    ) -> RelevanceEvaluation:
        duration = self._duration(selected_video)
        timestamps = tuple(
            sorted({round(min(duration - 0.04, max(0.0, duration * position)), 3) for position in (0.15, 0.5, 0.85)})
        )
        frames = [self._frame(selected_video, timestamp) for timestamp in timestamps]
        prompt = {
            "task": "Evaluate the visual relevance of the actual selected video frames to the narrated scene. Do not evaluate a search query.",
            "narration": narration_text,
            "scene_meaning": {
                "central_concept": intent.get("central_concept"),
                "interpretation": intent.get("interpretation"),
                "intent": intent.get("intent"),
                "entities": intent.get("entities", []),
                "actions": intent.get("actions", []),
                "metaphor": intent.get("metaphor"),
            },
            "scoring": "Return a JSON object with score between 0 and 100 (0 unrelated, 100 directly and clearly supports the narration) and a concise evidence-based rationale. Do not infer unseen video content.",
        }
        user_content: list[dict[str, object]] = [
            {"type": "text", "text": json.dumps(prompt, ensure_ascii=False)}
        ]
        for timestamp, image in zip(timestamps, frames, strict=True):
            user_content.append(
                {
                    "type": "text",
                    "text": f"Actual selected-footage frame sampled at {timestamp:.3f}s:",
                }
            )
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{base64.b64encode(image).decode('ascii')}"},
                }
            )
        try:
            response = httpx.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are an independent visual-relevance evaluator. Assess the supplied actual footage frames against the full narration meaning; do not use search metadata as visual evidence.",
                        },
                        {"role": "user", "content": user_content},
                    ],
                },
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
            result = json.loads(content) if isinstance(content, str) else content
            raw_score = float(result["score"])
            rationale = result["rationale"]
            if not 0 <= raw_score <= 100 or not isinstance(rationale, str) or not rationale.strip():
                raise ValueError("Evaluator score or rationale is invalid.")
            resolved_model = str(payload.get("model") or self.model)
        except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError) as exc:
            raise RelevanceEvaluationError(
                "Configured vision-language evaluator did not return a valid score."
            ) from exc
        return RelevanceEvaluation(
            provider=self.provider,
            model=resolved_model,
            version=resolved_model,
            score=round(raw_score / 100, 4),
            rationale=rationale.strip()[:2000],
            sampled_timestamps_seconds=timestamps,
        )

    def _duration(self, video_path: Path) -> float:
        try:
            result = subprocess.run(
                [
                    self.ffprobe_binary,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(video_path),
                ],
                capture_output=True,
                check=False,
                timeout=min(self.timeout_seconds, 60.0),
            )
            duration = float(result.stdout.decode("ascii").strip())
            if result.returncode != 0 or duration <= 0:
                raise ValueError("Invalid video duration.")
            return duration
        except (FileNotFoundError, subprocess.TimeoutExpired, UnicodeDecodeError, ValueError) as exc:
            raise RelevanceEvaluationError(
                "Selected footage could not be probed for visual evaluation."
            ) from exc

    def _frame(self, video_path: Path, timestamp: float) -> bytes:
        try:
            result = subprocess.run(
                [
                    self.ffmpeg_binary,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{timestamp:.3f}",
                    "-i",
                    str(video_path),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=512:-2",
                    "-f",
                    "image2pipe",
                    "-vcodec",
                    "mjpeg",
                    "pipe:1",
                ],
                capture_output=True,
                check=False,
                timeout=min(self.timeout_seconds, 60.0),
            )
            if result.returncode != 0 or not result.stdout:
                raise ValueError("No frame was extracted.")
            return result.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError) as exc:
            raise RelevanceEvaluationError(
                "Selected footage frame extraction failed."
            ) from exc


def configured_relevance_evaluator(settings: Settings) -> VisionLanguageEvaluator | None:
    if settings.relevance_evaluator_provider == "disabled":
        return None
    if settings.relevance_evaluator_provider == "openai_compatible":
        return OpenAICompatibleVisionEvaluator(
            base_url=settings.relevance_evaluator_base_url,
            api_key=settings.relevance_evaluator_api_key,
            model=settings.relevance_evaluator_model,
            ffmpeg_binary=settings.ffmpeg_binary,
            ffprobe_binary=settings.ffprobe_binary,
            timeout_seconds=settings.relevance_evaluator_timeout_seconds,
        )
    raise ValueError(
        f"Unsupported relevance evaluator provider: {settings.relevance_evaluator_provider}"
    )
