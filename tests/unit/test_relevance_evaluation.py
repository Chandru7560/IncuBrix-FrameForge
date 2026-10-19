from __future__ import annotations

import base64
import json

import pytest

from scenepilot.config import ConfigurationError, Settings
from scenepilot.services.relevance_evaluation import (
    OpenAICompatibleVisionEvaluator,
    RelevanceEvaluationError,
    configured_relevance_evaluator,
)


def test_disabled_evaluator_is_a_safe_default(settings):
    assert configured_relevance_evaluator(settings) is None


def test_openai_compatible_evaluator_uses_selected_video_frames_and_normalizes_score(
    monkeypatch, tmp_path
):
    video = tmp_path / "selected.mp4"
    video.write_bytes(b"video")
    evaluator = OpenAICompatibleVisionEvaluator(
        base_url="https://vision.example/v1",
        api_key="test-secret",
        model="vision-model",
        ffmpeg_binary="ffmpeg",
        ffprobe_binary="ffprobe",
        timeout_seconds=5,
    )
    monkeypatch.setattr(evaluator, "_duration", lambda path: 10.0)
    monkeypatch.setattr(evaluator, "_frame", lambda path, timestamp: f"frame-{timestamp}".encode())
    request_data = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "model": "vision-model-v2",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"score": 85, "rationale": "Frames show the narrated subject."}
                            )
                        }
                    }
                ],
            }

    def fake_post(url, *, headers, json, timeout):
        request_data.update(url=url, headers=headers, payload=json, timeout=timeout)
        return Response()

    monkeypatch.setattr("scenepilot.services.relevance_evaluation.httpx.post", fake_post)
    result = evaluator.evaluate(
        narration_text="A flock of birds crosses the wetland.",
        intent={"central_concept": "bird migration", "actions": ["crosses"]},
        selected_video=video,
    )
    assert result.score == 0.85
    assert result.model == "vision-model-v2"
    assert result.sampled_timestamps_seconds == (1.5, 5.0, 8.5)
    user_message = request_data["payload"]["messages"][1]["content"]
    image_parts = [part for part in user_message if part["type"] == "image_url"]
    assert len(image_parts) == 3
    assert base64.b64decode(image_parts[0]["image_url"]["url"].split(",", 1)[1]) == b"frame-1.5"
    assert "actual footage frames" in request_data["payload"]["messages"][0]["content"]
    assert "test-secret" == request_data["headers"]["Authorization"].removeprefix("Bearer ")


def test_evaluator_invalid_score_is_not_normalized_or_returned(monkeypatch, tmp_path):
    evaluator = OpenAICompatibleVisionEvaluator(
        base_url="https://vision.example/v1",
        api_key="test-secret",
        model="vision-model",
        ffmpeg_binary="ffmpeg",
        ffprobe_binary="ffprobe",
        timeout_seconds=5,
    )
    monkeypatch.setattr(evaluator, "_duration", lambda path: 1.0)
    monkeypatch.setattr(evaluator, "_frame", lambda path, timestamp: b"frame")

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "choices": [
                    {"message": {"content": '{"score": 101, "rationale": "invalid range"}'}}
                ]
            }

    monkeypatch.setattr(
        "scenepilot.services.relevance_evaluation.httpx.post",
        lambda *args, **kwargs: Response(),
    )
    with pytest.raises(RelevanceEvaluationError, match="valid score"):
        evaluator.evaluate(
            narration_text="Narration",
            intent={},
            selected_video=tmp_path / "selected.mp4",
        )


def test_configured_evaluator_requires_secure_endpoint_and_credentials(monkeypatch):
    monkeypatch.setenv("RELEVANCE_EVALUATOR_PROVIDER", "openai_compatible")
    monkeypatch.setenv("RELEVANCE_EVALUATOR_BASE_URL", "http://remote.example/v1")
    monkeypatch.setenv("RELEVANCE_EVALUATOR_MODEL", "model")
    monkeypatch.setenv("RELEVANCE_EVALUATOR_API_KEY", "key")
    with pytest.raises(ConfigurationError, match="HTTPS"):
        Settings.from_env()
