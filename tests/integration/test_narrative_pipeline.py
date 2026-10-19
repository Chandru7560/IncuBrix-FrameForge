from __future__ import annotations

import math
import struct
import wave
from dataclasses import replace
from pathlib import Path

import pytest

from scenepilot.providers.narration import (
    ProviderUnavailableError,
    SynthesisResult,
    TranscriptSegment,
    TranscriptionResult,
)
from scenepilot.services.narrative_pipeline import NarrativePipelineError, NarrativeScenePipeline
from scenepilot.services.relevance_evaluation import (
    RelevanceEvaluation,
    RelevanceEvaluationError,
)


def write_wav(path: Path, *, duration_seconds: float, tone: bool) -> None:
    sample_rate = 8000
    frame_count = int(sample_rate * duration_seconds)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        for start in range(0, frame_count, 2048):
            count = min(2048, frame_count - start)
            frames = bytearray()
            for index in range(start, start + count):
                sample = int(3000 * math.sin(2 * math.pi * 440 * index / sample_rate)) if tone else 0
                frames.extend(struct.pack("<h", sample))
            audio.writeframesraw(frames)


class FixedSpeechRecognizer:
    def __init__(self, result: TranscriptionResult | None = None, error=None) -> None:
        self.result = result
        self.error = error

    def transcribe(self, audio_path, probe):
        if self.error:
            raise self.error
        return self.result


class FixedTTS:
    def __init__(self, duration_seconds: float = 4.25, error=None) -> None:
        self.duration_seconds = duration_seconds
        self.error = error

    def synthesize(self, text, voice, output_path):
        if self.error:
            raise self.error
        write_wav(output_path, duration_seconds=self.duration_seconds, tone=True)
        return SynthesisResult(
            audio_path=output_path,
            provider="test",
            model="fixed-duration-test-tts",
        )


def pipeline(settings, **adapters):
    return NarrativeScenePipeline(settings, **adapters)


def audio_request(path: Path) -> dict:
    return {
        "upload": {
            "storage_path": str(path),
            "filename": path.name,
            "format": path.suffix.lstrip("."),
        },
        "style": "observational",
        "avoid": ["logos"],
    }


def test_script_timing_uses_measured_synthesized_audio_duration(settings, tmp_path):
    tts = FixedTTS(duration_seconds=4.25)
    result = pipeline(settings, tts_adapter=tts).process(
        "script",
        {"script": "Cloud computing stores useful data.", "voice": "test", "avoid": ["logos"]},
    )
    assert result["narration_duration_seconds"] == 4.25
    assert result["scene_plan"]["duration_seconds"] == 4.25
    assert result["scene_plan"]["timing_authority"] == "measured_generated_audio"
    assert result["scene_plan"]["scenes"][-1]["end_seconds"] == 4.25
    assert result["transcript"]["original_text"] == "Cloud computing stores useful data."
    assert "SCENE_PLANNING_ONLY" in " ".join(result["warnings"])
    assert list(settings.input_directory.glob("mock-tts-*.wav")) == []


def test_rendering_never_treats_silent_mock_tts_as_video_narration(settings):
    render_settings = replace(settings, render_enabled=True)
    with pytest.raises(NarrativePipelineError) as error:
        pipeline(render_settings).process("script", {"script": "A real video is required."})
    assert error.value.code == "REAL_NARRATION_REQUIRED"
    assert "actual narration audio track" in " ".join(error.value.warnings)


def test_rendering_fails_instead_of_claiming_success_without_eligible_footage(settings):
    render_settings = replace(settings, render_enabled=True)
    with pytest.raises(NarrativePipelineError) as error:
        pipeline(render_settings, tts_adapter=FixedTTS()).process(
            "script", {"script": "A licensed asset is required."}
        )
    assert error.value.code == "SCENE_ASSET_MISSING"


@pytest.mark.parametrize(
    "provider_error",
    [TimeoutError("test provider outage"), ProviderUnavailableError("provider not configured")],
)
def test_tts_provider_failure_uses_explicit_silent_timing_fallback(settings, provider_error):
    result = pipeline(
        settings,
        tts_adapter=FixedTTS(error=provider_error),
    ).process("script", {"script": "Trust is earned slowly."})
    warnings = " ".join(result["warnings"])
    assert "deterministic silent timing-fixture fallback was used" in warnings
    assert "not speech" in warnings
    assert result["scene_plan"]["coverage"]["coverage_ratio"] == 1


def test_structured_scene_order_and_visual_hints_are_preserved(settings):
    scenes = [
        {"text": "The bank approved a loan.", "visual_hint": "Account adviser at a desk"},
        {"text": "The river bank grew greener.", "visual_hint": "Trees beside flowing water"},
    ]
    result = pipeline(settings).process("scenes", {"scenes": scenes})
    planned = result["scene_plan"]["scenes"]
    assert [scene["order"] for scene in planned] == [1, 2]
    assert [scene["narration_text"] for scene in planned] == [scene["text"] for scene in scenes]
    assert [scene["intent_graph"]["visual_hint"] for scene in planned] == [
        scene["visual_hint"] for scene in scenes
    ]
    assert planned[0]["intent_graph"]["central_concept"] == "financial bank"
    assert planned[1]["intent_graph"]["central_concept"] == "river bank"
    assert result["scene_plan"]["coverage"]["coverage_ratio"] == 1


def test_independent_evaluation_is_attached_to_selected_video_not_search_query(
    settings, tmp_path
):
    footage_directory = settings.footage_directory
    footage_directory.mkdir(parents=True, exist_ok=True)
    (footage_directory / "selected.mp4").write_bytes(b"selected asset")

    class Retrieval:
        def select(self, scene, **kwargs):
            return {
                "status": "selected",
                "aspect_ratio": kwargs["aspect_ratio"],
                "overall_score": 0.72,
                "selected_shot_type": "wide",
                "selection_rationale": ["Metadata ranking only."],
                "rights_passport": {
                    "asset_id": "selected-asset",
                    "provider": "fixture-provider",
                    "downloaded_asset_key": "selected.mp4",
                    "commercial_use_verification_status": "verified",
                    "watermark_verification": "clear",
                },
            }

    class Evaluator:
        provider = "test-vlm"
        model = "test-vlm-v1"

        def evaluate(self, *, narration_text, intent, selected_video):
            assert selected_video == footage_directory / "selected.mp4"
            assert narration_text
            assert intent["central_concept"]
            return RelevanceEvaluation(
                provider=self.provider,
                model=self.model,
                version=self.model,
                score=0.83,
                rationale="The selected frame depicts the narrated action.",
                sampled_timestamps_seconds=(0.2, 1.0, 1.8),
            )

    result = NarrativeScenePipeline(
        settings,
        visual_retrieval=Retrieval(),
        relevance_evaluator=Evaluator(),
    ).process("script", {"script": "A team shares knowledge across a connected network."})
    evaluation = result["scene_plan"]["scenes"][0]["visual_retrieval"]["by_aspect_ratio"]["16:9"][
        "independent_relevance_evaluation"
    ]
    assert evaluation["status"] == "evaluated"
    assert evaluation["score"] == 0.83
    assert evaluation["compared_against"] == "selected_asset_video_frames"
    assert evaluation["rationale"] == "The selected frame depicts the narrated action."


def test_optional_evaluator_failure_leaves_score_unavailable_with_warning(
    settings, tmp_path
):
    footage_directory = settings.footage_directory
    footage_directory.mkdir(parents=True, exist_ok=True)
    (footage_directory / "selected.mp4").write_bytes(b"selected asset")

    class Retrieval:
        def select(self, scene, **kwargs):
            return {
                "status": "selected",
                "aspect_ratio": kwargs["aspect_ratio"],
                "overall_score": 0.5,
                "selected_shot_type": "wide",
                "selection_rationale": [],
                "rights_passport": {"downloaded_asset_key": "selected.mp4"},
            }

    class Evaluator:
        provider = "test-vlm"
        model = "unavailable-model"

        def evaluate(self, **kwargs):
            raise RelevanceEvaluationError("Evaluator endpoint timed out.")

    result = NarrativeScenePipeline(
        settings,
        visual_retrieval=Retrieval(),
        relevance_evaluator=Evaluator(),
    ).process("script", {"script": "A careful scene remains meaningful."})
    evaluation = result["scene_plan"]["scenes"][0]["visual_retrieval"]["by_aspect_ratio"]["16:9"][
        "independent_relevance_evaluation"
    ]
    assert evaluation["status"] == "unavailable"
    assert evaluation["score"] is None
    assert "RELEVANCE_EVALUATION_UNAVAILABLE" in " ".join(result["warnings"])


def test_uploaded_audio_preserves_original_transcript_and_provider_timestamps(
    settings, tmp_path
):
    source = tmp_path / "spoken.wav"
    write_wav(source, duration_seconds=6, tone=True)
    transcript = TranscriptionResult(
        original_text="Cloud computing moves useful data.",
        segments=(
            TranscriptSegment("Cloud computing", 0.2, 2.5, confidence=0.98),
            TranscriptSegment("moves useful data.", 2.6, 5.8, confidence=0.95),
        ),
        provider="test-stt",
        model="timestamp-test",
        warnings=("Test recognition warning.",),
    )
    result = pipeline(
        settings,
        speech_adapter=FixedSpeechRecognizer(result=transcript),
    ).process("audio_upload", audio_request(source))
    assert result["transcript"]["original_text"] == transcript.original_text
    assert result["transcript"]["provider"] == "test-stt"
    assert result["transcript"]["segments"][0]["start_seconds"] == 0.2
    assert result["scene_plan"]["timing_authority"] == "source_audio"
    assert result["narration_duration_seconds"] == 6
    assert "Test recognition warning." in result["warnings"]


def test_recognized_multiword_phrase_times_expand_at_sentence_boundaries(settings, tmp_path):
    source = tmp_path / "spoken.wav"
    write_wav(source, duration_seconds=4, tone=True)
    transcript = TranscriptionResult(
        original_text="Cloud computing runs. Data moves.",
        segments=(TranscriptSegment("Cloud computing runs. Data moves.", 0, 4),),
        provider="test-stt",
        model="phrase-timestamp-test",
    )
    result = pipeline(
        settings,
        speech_adapter=FixedSpeechRecognizer(result=transcript),
    ).process("audio_upload", audio_request(source))

    timed_tokens = result["scene_plan"]["transcript"]["segments"]
    assert [token["text"] for token in timed_tokens] == [
        "Cloud",
        "computing",
        "runs.",
        "Data",
        "moves.",
    ]
    assert all(
        token["timing_source"] == "estimated_within_recognized_phrase"
        for token in timed_tokens
    )
    scenes = result["scene_plan"]["scenes"]
    assert len(scenes) == 2
    assert scenes[0]["narration_text"] == "Cloud computing runs."
    assert scenes[1]["narration_text"] == "Data moves."
    assert result["scene_plan"]["coverage"]["coverage_ratio"] == 1
    assert any(
        "internal word times were apportioned" in reason
        for reason in result["scene_plan"]["fallback_reasons"]
    )


def test_silent_audio_is_detected_before_recognition(settings, tmp_path):
    source = tmp_path / "silence.wav"
    write_wav(source, duration_seconds=2, tone=False)
    with pytest.raises(NarrativePipelineError) as error:
        pipeline(settings, speech_adapter=FixedSpeechRecognizer(error=AssertionError())).process(
            "audio_upload", audio_request(source)
        )
    assert error.value.code == "SILENT_AUDIO"


def test_music_only_audio_is_reported_without_claiming_transcription(settings, tmp_path):
    source = tmp_path / "music-only.wav"
    write_wav(source, duration_seconds=2, tone=True)
    recognizer = FixedSpeechRecognizer(
        result=TranscriptionResult(
            original_text="",
            segments=(),
            provider="test-stt",
            model="music-condition-test",
            condition="music_only",
            warnings=("No speech detected by test recognizer.",),
        )
    )
    with pytest.raises(NarrativePipelineError) as error:
        pipeline(settings, speech_adapter=recognizer).process(
            "audio_upload", audio_request(source)
        )
    assert error.value.code == "MUSIC_ONLY_AUDIO"
    assert "No speech detected" in " ".join(error.value.warnings)


@pytest.mark.parametrize(
    ("condition", "expected_code"),
    [
        ("music_only", "MUSIC_ONLY_AUDIO"),
        ("unsupported", "UNSUPPORTED_AUDIO"),
        ("silence", "SILENT_AUDIO"),
    ],
)
def test_recognizer_audio_condition_is_reported_without_fallback(
    settings, tmp_path, condition, expected_code
):
    source = tmp_path / "speech.wav"
    write_wav(source, duration_seconds=2, tone=True)
    result = TranscriptionResult(
        original_text="",
        segments=(),
        provider="test-stt",
        model="condition-test",
        warnings=("Provider condition recorded.",),
        condition=condition,
    )
    with pytest.raises(NarrativePipelineError) as error:
        pipeline(
            settings,
            speech_adapter=FixedSpeechRecognizer(result=result),
        ).process("audio_upload", audio_request(source))
    assert error.value.code == expected_code


def test_missing_speech_provider_fails_without_fabricating_transcript(settings, tmp_path):
    source = tmp_path / "speech.wav"
    write_wav(source, duration_seconds=2, tone=True)
    with pytest.raises(NarrativePipelineError) as error:
        pipeline(settings).process("audio_upload", audio_request(source))
    assert error.value.code == "SPEECH_RECOGNITION_UNAVAILABLE"
    assert "no transcript was fabricated" in error.value.message
    assert error.value.warnings


def test_recognizer_failure_is_explicit_and_records_fallback_reason(settings, tmp_path):
    source = tmp_path / "speech.wav"
    write_wav(source, duration_seconds=2, tone=True)
    with pytest.raises(NarrativePipelineError) as error:
        pipeline(
            settings,
            speech_adapter=FixedSpeechRecognizer(error=TimeoutError("provider timeout")),
        ).process("audio_upload", audio_request(source))
    assert error.value.code == "SPEECH_RECOGNITION_FAILED"
    assert "no transcript was fabricated" in error.value.message
    assert "TimeoutError" in error.value.warnings[0]


def test_corrupted_audio_is_reported_as_corrupted(settings, tmp_path):
    source = tmp_path / "corrupted.wav"
    source.write_bytes(b"RIFF\x00\x00")
    with pytest.raises(NarrativePipelineError) as error:
        pipeline(settings).process("audio_upload", audio_request(source))
    assert error.value.code == "CORRUPTED_AUDIO"


def test_invalid_provider_timestamps_fail_explicitly(settings, tmp_path):
    source = tmp_path / "spoken.wav"
    write_wav(source, duration_seconds=3, tone=True)
    transcript = TranscriptionResult(
        original_text="A timestamped sentence.",
        segments=(TranscriptSegment("A timestamped sentence.", 0, 5),),
        provider="test-stt",
        model="invalid-time-test",
    )
    with pytest.raises(NarrativePipelineError) as error:
        pipeline(
            settings,
            speech_adapter=FixedSpeechRecognizer(result=transcript),
        ).process("audio_upload", audio_request(source))
    assert error.value.code == "INVALID_TRANSCRIPTION_TIMESTAMPS"


def test_audio_url_is_downloaded_validated_and_transcribed(settings, tmp_path):
    downloaded_paths = []
    transcript = TranscriptionResult(
        original_text="A public voiceover is validated.",
        segments=(TranscriptSegment("A public voiceover is validated.", 0, 4),),
        provider="test-stt",
        model="url-test",
    )

    def fetch_audio(url, *, destination, **kwargs):
        assert url == "https://example.com/voice.wav"
        assert kwargs["max_bytes"] == settings.max_upload_bytes
        write_wav(destination, duration_seconds=4, tone=True)
        downloaded_paths.append(destination)
        return destination.stat().st_size

    result = pipeline(
        settings,
        speech_adapter=FixedSpeechRecognizer(result=transcript),
        audio_url_fetcher=fetch_audio,
    ).process("audio_url", {"audio_url": "https://example.com/voice.wav"})

    assert result["transcript"]["original_text"] == transcript.original_text
    assert result["scene_plan"]["source_type"] == "audio_url"
    assert result["scene_plan"]["coverage"]["coverage_ratio"] == 1
    assert len(downloaded_paths) == 1
    assert not downloaded_paths[0].exists()


def test_audio_url_rejects_non_audio_download_and_cleans_temporary_file(settings):
    downloaded_paths = []

    def fetch_non_audio(url, *, destination, **kwargs):
        destination.write_bytes(b"<html>not audio</html>")
        downloaded_paths.append(destination)
        return destination.stat().st_size

    with pytest.raises(NarrativePipelineError) as error:
        pipeline(settings, audio_url_fetcher=fetch_non_audio).process(
            "audio_url", {"audio_url": "https://example.com/voice.wav"}
        )
    assert error.value.code == "UNSUPPORTED_AUDIO_FORMAT"
    assert downloaded_paths and not downloaded_paths[0].exists()


def test_unexpected_tts_errors_are_not_hidden_by_silent_mock_fallback(settings):
    with pytest.raises(ValueError, match="unexpected adapter bug"):
        pipeline(
            settings,
            tts_adapter=FixedTTS(error=ValueError("unexpected adapter bug")),
        ).process("script", {"script": "This error must remain visible."})
