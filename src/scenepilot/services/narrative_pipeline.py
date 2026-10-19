"""Input-specific speech, narration timing, and scene-planning orchestration."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from scenepilot.config import Settings
from scenepilot.errors import ApiError
from scenepilot.providers.ffprobe import FFprobeAudioProbe
from scenepilot.providers.narration import (
    AudioProbe,
    AudioProbeResult,
    DeterministicMockSpeechRecognition,
    DeterministicMockTextToSpeech,
    ProviderUnavailableError,
    SpeechRecognitionAdapter,
    SynthesisResult,
    TextToSpeechAdapter,
    TranscriptionResult,
    estimate_word_segments,
    probe_pcm_wav,
)
from scenepilot.services.input_validation import detect_audio_format, validate_audio_upload
from scenepilot.services.narrative_intent_graph import (
    DeterministicSemanticProvider,
    PlannerSettings,
    ScenePlanningError,
    SemanticProvider,
    build_scene_plan,
    normalize_script,
)
from scenepilot.services.relevance_evaluation import (
    RelevanceEvaluationError,
    VisionLanguageEvaluator,
    configured_relevance_evaluator,
)
from scenepilot.services.url_safety import fetch_public_audio
from scenepilot.services.visual_retrieval import VisualRetrievalService
from scenepilot.services.video_renderer import FFmpegRenderer, RenderingError

logger = logging.getLogger("scenepilot.narrative")


class NarrativePipelineError(RuntimeError):
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


class NarrativeScenePipeline:
    def __init__(
        self,
        settings: Settings,
        *,
        speech_adapter: SpeechRecognitionAdapter | None = None,
        tts_adapter: TextToSpeechAdapter | None = None,
        audio_probe: AudioProbe | None = None,
        semantic_provider: SemanticProvider | None = None,
        visual_retrieval: VisualRetrievalService | None = None,
        renderer: FFmpegRenderer | None = None,
        relevance_evaluator: VisionLanguageEvaluator | None = None,
        audio_url_fetcher: Callable[..., int] | None = None,
    ) -> None:
        self.settings = settings
        self.speech_adapter = speech_adapter or DeterministicMockSpeechRecognition()
        self.tts_adapter = tts_adapter or DeterministicMockTextToSpeech(
            settings.mock_tts_words_per_minute
        )
        self.audio_probe = audio_probe or FFprobeAudioProbe(
            settings.ffprobe_binary,
            settings.ffmpeg_binary,
            max_audio_seconds=settings.max_audio_seconds,
            silence_threshold_dbfs=settings.silence_threshold_dbfs,
        )
        self.semantic_provider = semantic_provider or DeterministicSemanticProvider()
        self.visual_retrieval = visual_retrieval or VisualRetrievalService(settings)
        self.renderer = renderer or FFmpegRenderer(settings)
        self.relevance_evaluator = (
            relevance_evaluator
            if relevance_evaluator is not None
            else configured_relevance_evaluator(settings)
        )
        self.audio_url_fetcher = audio_url_fetcher or fetch_public_audio
        self.planner_settings = PlannerSettings(
            min_scene_seconds=settings.min_scene_seconds,
            max_scene_seconds=settings.max_scene_seconds,
            boundary_tolerance_seconds=settings.narration_boundary_tolerance_seconds,
        )

    def process(self, input_type: str, request_data: dict) -> dict[str, object]:
        if input_type in {"script", "scenes"}:
            return self._process_script(input_type, request_data)
        if input_type == "audio_upload":
            return self._process_uploaded_audio(request_data)
        if input_type == "audio_url":
            return self._process_audio_url(request_data)
        raise NarrativePipelineError(
            "UNSUPPORTED_INPUT_TYPE",
            "The worker received an unsupported narration input type.",
        )

    def _process_script(self, input_type: str, request_data: dict) -> dict[str, object]:
        structured_scenes = request_data.get("scenes") if input_type == "scenes" else None
        if structured_scenes is not None:
            original_text = " ".join(str(scene["text"]) for scene in structured_scenes)
        else:
            original_text = str(request_data.get("script", ""))
        try:
            original_text, normalized_text = normalize_script(original_text)
        except ScenePlanningError as exc:
            raise NarrativePipelineError("EMPTY_SCRIPT", str(exc)) from exc

        provider_warnings: list[str] = []
        generated_wav = self.settings.input_directory / f"mock-tts-{uuid.uuid4()}.wav"
        try:
            generated_wav.parent.mkdir(parents=True, exist_ok=True)
            try:
                synthesis = self.tts_adapter.synthesize(
                    normalized_text,
                    str(request_data.get("voice", "en-IN-female")),
                    generated_wav,
                )
            except (ProviderUnavailableError, OSError) as exc:
                synthesis = self._mock_tts_fallback(
                    normalized_text,
                    request_data,
                    generated_wav,
                    exc,
                    provider_warnings,
                )
            synthesis_probe = probe_pcm_wav(
                synthesis.audio_path,
                silence_threshold_dbfs=self.settings.silence_threshold_dbfs,
            )
            provider_warnings.extend(synthesis.warnings)
            transcript_segments = estimate_word_segments(
                normalized_text,
                synthesis_probe.duration_seconds,
                source="known_script",
            )
            transcript = TranscriptionResult(
                original_text=original_text,
                segments=transcript_segments,
                provider="script_input",
                model="source_text",
                warnings=tuple(provider_warnings),
                condition="speech",
            )
            graph = self._plan(
                transcript,
                duration_seconds=synthesis_probe.duration_seconds,
                source_type=input_type,
                request_data=request_data,
                structured_scenes=structured_scenes,
            )
            graph["timing_authority"] = "measured_generated_audio"
            retrieval_warnings = self._apply_visual_retrieval(graph, request_data)
            provider_warnings.extend(retrieval_warnings)
            result = self._success_result(transcript, graph, synthesis_probe, provider_warnings)
            if self.settings.render_enabled:
                if synthesis.provider == "mock" or any(
                    warning.startswith("MOCK_TTS_SILENT_FIXTURE:")
                    for warning in synthesis.warnings
                ):
                    raise NarrativePipelineError(
                        "REAL_NARRATION_REQUIRED",
                        "A silent mock timing fixture cannot be used to produce a video.",
                        warnings=provider_warnings
                        + ["Rendering requires an actual narration audio track."],
                    )
                self._render_result(
                    result,
                    audio_path=synthesis.audio_path,
                    graph=graph,
                    request_data=request_data,
                    transcript=transcript,
                    warnings=provider_warnings,
                    tts_provider=synthesis.provider,
                    tts_model=synthesis.model,
                )
            else:
                result["warnings"].append(
                    "SCENE_PLANNING_ONLY: rendering is disabled; no video artifacts were created."
                )
            return result
        except ApiError as exc:
            raise self._from_api_error(exc, provider_warnings) from exc
        except ScenePlanningError as exc:
            raise NarrativePipelineError(
                "SCENE_PLAN_FAILED",
                "A complete, contiguous scene plan could not be produced.",
                warnings=provider_warnings + [f"Planning failure: {type(exc).__name__}"],
            ) from exc
        finally:
            generated_wav.unlink(missing_ok=True)

    def _process_audio_url(self, request_data: dict) -> dict[str, object]:
        audio_url = request_data.get("audio_url")
        if not isinstance(audio_url, str) or not audio_url:
            raise NarrativePipelineError(
                "INVALID_AUDIO_URL",
                "The audio URL is missing or invalid.",
            )

        audio_path = self.settings.input_directory / f"audio-url-{uuid.uuid4().hex}.download"
        try:
            self.settings.input_directory.mkdir(parents=True, exist_ok=True)
            self.audio_url_fetcher(
                audio_url,
                destination=audio_path,
                max_bytes=self.settings.max_upload_bytes,
                timeout_seconds=self.settings.url_fetch_timeout_seconds,
                max_redirects=self.settings.url_fetch_max_redirects,
                allow_hosts=self.settings.allowed_audio_hosts,
            )
            audio_bytes = audio_path.read_bytes()
            detected_format = detect_audio_format(audio_bytes)
            if detected_format is None:
                raise ApiError(
                    415,
                    "UNSUPPORTED_AUDIO_FORMAT",
                    "The remote URL did not return a supported audio file.",
                )
            validated_audio_path = audio_path.with_suffix(f".{detected_format}")
            audio_path.replace(validated_audio_path)
            audio_path = validated_audio_path
            content_type = {
                "mp3": "audio/mpeg",
                "wav": "audio/wav",
                "m4a": "audio/mp4",
            }[detected_format]
            validate_audio_upload(
                f"remote-audio.{detected_format}",
                content_type,
                audio_bytes,
                max_upload_bytes=self.settings.max_upload_bytes,
                max_audio_seconds=self.settings.max_audio_seconds,
            )
            request_data = {
                **request_data,
                "upload": {
                    "storage_path": str(audio_path),
                    "filename": f"remote-audio.{detected_format}",
                    "format": detected_format,
                },
            }
            return self._process_uploaded_audio(request_data, source_type="audio_url")
        except ApiError as exc:
            raise self._from_api_error(exc) from exc
        except OSError as exc:
            raise NarrativePipelineError(
                "AUDIO_URL_STORAGE_FAILED",
                "The downloaded audio could not be read from temporary storage.",
                warnings=[f"Audio URL processing failed with {type(exc).__name__}."],
            ) from exc
        finally:
            audio_path.unlink(missing_ok=True)

    def _process_uploaded_audio(
        self,
        request_data: dict,
        *,
        source_type: str = "audio_upload",
    ) -> dict[str, object]:
        upload = request_data.get("upload", {})
        audio_path = Path(str(upload.get("storage_path", "")))
        if not audio_path.is_file():
            raise NarrativePipelineError(
                "AUDIO_INPUT_MISSING",
                "The uploaded audio file is no longer available to the worker.",
            )
        try:
            probe = self._probe_audio(audio_path)
        except ApiError as exc:
            raise self._from_api_error(exc) from exc
        if probe.condition == "silence":
            raise NarrativePipelineError(
                "SILENT_AUDIO",
                "The uploaded audio contains no detectable speech-level signal.",
                warnings=["Audio probe measured silence before speech recognition."],
                details={"duration_seconds": probe.duration_seconds, "rms_dbfs": probe.rms_dbfs},
            )
        try:
            transcription = self.speech_adapter.transcribe(audio_path, probe)
        except ProviderUnavailableError as exc:
            raise NarrativePipelineError(
                "SPEECH_RECOGNITION_UNAVAILABLE",
                "Speech recognition is not configured; no transcript was fabricated.",
                warnings=[
                    f"Recognition fallback reason: {type(self.speech_adapter).__name__} "
                    "cannot transcribe uploaded audio."
                ],
            ) from exc
        except ApiError as exc:
            raise self._from_api_error(exc) from exc
        except Exception as exc:
            raise NarrativePipelineError(
                "SPEECH_RECOGNITION_FAILED",
                "The configured speech-recognition adapter failed; no transcript was fabricated.",
                warnings=[
                    f"Recognition fallback reason: {type(self.speech_adapter).__name__} "
                    f"failed with {type(exc).__name__}; transcription cannot be safely inferred."
                ],
            ) from exc
        if transcription.condition in {"silence", "empty"}:
            raise NarrativePipelineError(
                "SILENT_AUDIO",
                "No speech was detected in the uploaded audio.",
                warnings=list(transcription.warnings),
            )
        if transcription.condition == "music_only":
            raise NarrativePipelineError(
                "MUSIC_ONLY_AUDIO",
                "The uploaded audio contains music but no transcribed speech.",
                warnings=list(transcription.warnings),
            )
        if transcription.condition == "unsupported":
            raise NarrativePipelineError(
                "UNSUPPORTED_AUDIO",
                "The speech recognition provider cannot process this audio input.",
                warnings=list(transcription.warnings),
            )
        if not transcription.original_text.strip() or not transcription.segments:
            raise NarrativePipelineError(
                "NO_SPEECH_DETECTED",
                "The speech recognition provider returned no transcript segments.",
                warnings=list(transcription.warnings),
            )
        warnings = list(transcription.warnings)
        try:
            graph = self._plan(
                transcription,
                duration_seconds=probe.duration_seconds,
                source_type=source_type,
                request_data=request_data,
                structured_scenes=None,
            )
        except ScenePlanningError as exc:
            warnings.append(
                "Timestamp validation fallback reason: recognition segments were not usable; "
                "scene timing could not be safely recovered."
            )
            raise NarrativePipelineError(
                "INVALID_TRANSCRIPTION_TIMESTAMPS",
                str(exc),
                warnings=warnings,
            ) from exc
        graph["timing_authority"] = "source_audio"
        warnings.extend(self._apply_visual_retrieval(graph, request_data))
        result = self._success_result(transcription, graph, probe, warnings)
        if self.settings.render_enabled:
            self._render_result(
                result,
                audio_path=audio_path,
                graph=graph,
                request_data=request_data,
                transcript=transcription,
                warnings=warnings,
                tts_provider=None,
                tts_model=None,
            )
        else:
            result["warnings"].append(
                "SCENE_PLANNING_ONLY: rendering is disabled; no video artifacts were created."
            )
        return result

    def _apply_visual_retrieval(
        self,
        graph: dict[str, object],
        request_data: dict,
    ) -> list[str]:
        warnings: list[str] = []
        aspect_ratios = request_data.get("aspect_ratios") or ["16:9"]
        all_scenes = graph.get("scenes")
        if not isinstance(all_scenes, list):
            return ["VISUAL_RETRIEVAL_SKIPPED: scene plan contained no scene records."]
        by_ratio: dict[str, set[str]] = {ratio: set() for ratio in aspect_ratios}
        previous_shot: dict[str, str | None] = {ratio: None for ratio in aspect_ratios}
        for scene in all_scenes:
            if not isinstance(scene, dict):
                continue
            selections = {}
            for ratio in aspect_ratios:
                selection = self.visual_retrieval.select(
                    scene,
                    aspect_ratio=ratio,
                    avoid=list(request_data.get("avoid", [])),
                    selected_asset_ids=by_ratio[ratio],
                    previous_shot_type=previous_shot[ratio],
                    visual_style=str(request_data.get("style", "")),
                )
                selections[ratio] = selection
                if selection["status"] == "selected":
                    previous_shot[ratio] = selection.get("selected_shot_type")
                    self._evaluate_selected_footage(
                        scene,
                        selection,
                        warnings,
                    )
                else:
                    warnings.append(
                        f"VISUAL_RETRIEVAL_NO_ELIGIBLE_ASSET: {scene.get('scene_id')} "
                        f"({ratio}) has no candidate that passed configured relevance, "
                        "safety, technical, and rights checks."
                    )
            scene["visual_retrieval"] = {"by_aspect_ratio": selections}
        return list(dict.fromkeys(warnings))

    def _evaluate_selected_footage(
        self,
        scene: dict[str, object],
        selection: dict[str, object],
        warnings: list[str],
    ) -> None:
        passport = selection.get("rights_passport")
        if not isinstance(passport, dict):
            return
        if self.relevance_evaluator is None:
            selection["independent_relevance_evaluation"] = {
                "status": "disabled",
                "provider": None,
                "model": None,
                "version": None,
                "score": None,
                "normalization": "model_score_0_to_100_divided_by_100",
                "rationale": "No independent evaluator is configured.",
                "compared_against": "selected_asset_video_frames",
            }
            return
        asset_key = passport.get("downloaded_asset_key")
        asset_path = self.settings.footage_directory / str(asset_key) if asset_key else None
        if asset_path is None or not asset_path.is_file():
            selection["independent_relevance_evaluation"] = {
                "status": "unavailable",
                "provider": self.relevance_evaluator.provider,
                "model": self.relevance_evaluator.model,
                "version": None,
                "score": None,
                "normalization": "model_score_0_to_100_divided_by_100",
                "rationale": "The selected asset file is not available to the configured evaluator.",
                "compared_against": "selected_asset_video_frames",
            }
            warnings.append(
                f"RELEVANCE_EVALUATION_UNAVAILABLE: {scene.get('scene_id')} selected footage file is unavailable."
            )
            return
        try:
            evaluation = self.relevance_evaluator.evaluate(
                narration_text=str(scene.get("narration_text") or ""),
                intent=scene["intent_graph"],
                selected_video=asset_path,
            )
        except RelevanceEvaluationError as exc:
            logger.warning(
                "independent_relevance_evaluation_unavailable",
                extra={
                    "job_id": None,
                    "error_type": type(exc).__name__,
                    "scene_id": scene.get("scene_id"),
                },
            )
            selection["independent_relevance_evaluation"] = {
                "status": "unavailable",
                "provider": self.relevance_evaluator.provider,
                "model": self.relevance_evaluator.model,
                "version": None,
                "score": None,
                "normalization": "model_score_0_to_100_divided_by_100",
                "rationale": str(exc),
                "compared_against": "selected_asset_video_frames",
            }
            warnings.append(
                f"RELEVANCE_EVALUATION_UNAVAILABLE: {scene.get('scene_id')} independent visual evaluation failed."
            )
            return
        selection["independent_relevance_evaluation"] = {
            "status": "evaluated",
            "provider": evaluation.provider,
            "model": evaluation.model,
            "version": evaluation.version,
            "score": evaluation.score,
            "normalization": evaluation.normalization,
            "rationale": evaluation.rationale,
            "sampled_timestamps_seconds": list(evaluation.sampled_timestamps_seconds),
            "compared_against": "selected_asset_video_frames",
        }

    def _render_result(
        self,
        result: dict[str, object],
        *,
        audio_path: Path,
        graph: dict[str, object],
        request_data: dict,
        transcript: TranscriptionResult,
        warnings: list[str],
        tts_provider: str | None,
        tts_model: str | None,
    ) -> None:
        try:
            rendered = self.renderer.render(
                job_id=str(request_data.get("_job_id") or uuid.uuid4()),
                narration_path=audio_path,
                scene_plan=graph,
                aspect_ratios=list(request_data.get("aspect_ratios") or ["16:9"]),
                captions=request_data.get("captions"),
                warnings=warnings,
                providers={
                    "speech_recognition": transcript.provider,
                    "transcription_model": transcript.model,
                    "tts": tts_provider,
                    "tts_model": tts_model,
                },
            )
        except RenderingError as exc:
            raise NarrativePipelineError(
                exc.code,
                exc.message,
                warnings=warnings + exc.warnings,
                details=exc.details,
            ) from exc
        result["warnings"].extend(
            warning
            for warning in rendered.manifest["warnings"]
            if warning not in result["warnings"]
        )
        result["artifact_names"] = list(rendered.artifact_names)

    def _probe_audio(self, audio_path: Path) -> AudioProbeResult:
        if audio_path.suffix.lower() == ".wav":
            try:
                return probe_pcm_wav(
                    audio_path,
                    silence_threshold_dbfs=self.settings.silence_threshold_dbfs,
                )
            except ApiError:
                raise
            except OSError as exc:
                raise NarrativePipelineError(
                    "CORRUPTED_AUDIO",
                    "The WAV file could not be read by the worker.",
                    warnings=[f"Audio probe failed with {type(exc).__name__}."],
                ) from exc
        try:
            return self.audio_probe.probe(audio_path)
        except ApiError as exc:
            raise self._from_api_error(exc) from exc

    def _mock_tts_fallback(
        self,
        text: str,
        request_data: dict,
        output_path: Path,
        provider_error: Exception,
        warnings: list[str],
    ) -> SynthesisResult:
        if isinstance(self.tts_adapter, DeterministicMockTextToSpeech):
            raise NarrativePipelineError(
                "TEXT_TO_SPEECH_FAILED",
                "The configured mock TTS timing fixture could not be generated.",
                warnings=warnings + [f"TTS failure: {type(provider_error).__name__}"],
            ) from provider_error
        reason = (
            f"TTS provider {type(self.tts_adapter).__name__} failed with "
            f"{type(provider_error).__name__}; deterministic silent timing-fixture fallback was used."
        )
        warnings.append(reason)
        fallback = DeterministicMockTextToSpeech(self.settings.mock_tts_words_per_minute)
        try:
            synthesis = fallback.synthesize(
                text,
                str(request_data.get("voice", "en-IN-female")),
                output_path,
            )
        except OSError as exc:
            raise NarrativePipelineError(
                "TEXT_TO_SPEECH_FAILED",
                "The TTS provider and deterministic timing-fixture fallback both failed.",
                warnings=warnings + [f"Fallback failure: {type(exc).__name__}"],
            ) from exc
        return synthesis

    def _plan(
        self,
        transcript: TranscriptionResult,
        *,
        duration_seconds: float,
        source_type: str,
        request_data: dict,
        structured_scenes: list[dict[str, object]] | None,
    ) -> dict[str, object]:
        try:
            graph = build_scene_plan(
                transcript=transcript.original_text,
                transcript_segments=transcript.segments,
                duration_seconds=duration_seconds,
                source_type=source_type,
                semantic_provider=self.semantic_provider,
                structured_scenes=structured_scenes,
                visual_style=str(request_data.get("style", "")),
                avoid=list(request_data.get("avoid", [])),
                settings=self.planner_settings,
            )
        except ScenePlanningError:
            raise
        except Exception as exc:
            deterministic_graph = build_scene_plan(
                transcript=transcript.original_text,
                transcript_segments=transcript.segments,
                duration_seconds=duration_seconds,
                source_type=source_type,
                semantic_provider=DeterministicSemanticProvider(),
                structured_scenes=structured_scenes,
                visual_style=str(request_data.get("style", "")),
                avoid=list(request_data.get("avoid", [])),
                settings=self.planner_settings,
            )
            fallback_reason = (
                f"Scene-planning provider {type(self.semantic_provider).__name__} failed with "
                f"{type(exc).__name__}; deterministic planner fallback was applied."
            )
            deterministic_graph["fallback_reasons"].append(fallback_reason)
            deterministic_graph["warnings"].append("Deterministic scene-planning fallback was applied.")
            return deterministic_graph
        graph["transcript"]["provider"] = transcript.provider
        graph["transcript"]["model"] = transcript.model
        graph["transcript"]["warnings"] = list(transcript.warnings)
        return graph

    @staticmethod
    def _success_result(
        transcript: TranscriptionResult,
        graph: dict[str, object],
        probe: AudioProbeResult,
        warnings: list[str],
    ) -> dict[str, object]:
        warnings.extend(item for item in graph["warnings"] if item not in warnings)
        warnings.extend(item for item in transcript.warnings if item not in warnings)
        return {
            "warnings": warnings,
            "result_links": {},
            "transcript": {
                "original_text": transcript.original_text,
                "provider": transcript.provider,
                "model": transcript.model,
                "warnings": list(transcript.warnings),
                "segments": [
                    {
                        "text": segment.text,
                        "start_seconds": segment.start_seconds,
                        "end_seconds": segment.end_seconds,
                        "confidence": segment.confidence,
                    }
                    for segment in transcript.segments
                ],
            },
            "scene_plan": graph,
            "narration_duration_seconds": probe.duration_seconds,
            "analysis": {
                "audio_probe": asdict(probe),
                "scene_plan_sha256": hashlib.sha256(
                    json.dumps(graph, sort_keys=True, ensure_ascii=True).encode("utf-8")
                ).hexdigest(),
            },
        }

    @staticmethod
    def _from_api_error(error: ApiError, warnings: list[str] | None = None) -> NarrativePipelineError:
        return NarrativePipelineError(
            error.code,
            error.message,
            warnings=warnings or [],
            details=error.details,
        )
