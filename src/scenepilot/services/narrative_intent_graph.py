"""Deterministic, explainable narrative intent graph and timeline planner."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Protocol

from scenepilot.providers.narration import TranscriptSegment


class SemanticProvider(Protocol):
    def analyze(self, text: str) -> dict[str, object]: ...


class DeterministicSemanticProvider:
    """Small lexical/context rules for local use; no external model is implied."""

    _finance_terms = {
        "account",
        "accounts",
        "bank",
        "cash",
        "cost",
        "credit",
        "deposit",
        "finance",
        "financial",
        "invest",
        "loan",
        "money",
        "price",
        "profit",
        "purchase",
        "revenue",
        "save",
        "savings",
        "spend",
    }
    _abstract_value_terms = {
        "abstract",
        "care",
        "human",
        "meaning",
        "principle",
        "relationship",
        "respect",
        "trust",
        "worth",
    }
    _computing_terms = {
        "app",
        "application",
        "compute",
        "computing",
        "data",
        "digital",
        "internet",
        "online",
        "server",
        "software",
        "storage",
        "technology",
    }
    _sky_terms = {
        "blue",
        "clouds",
        "forecast",
        "rain",
        "sky",
        "storm",
        "sun",
        "weather",
    }
    _river_terms = {
        "bank",
        "current",
        "flood",
        "river",
        "shore",
        "stream",
        "water",
    }
    _device_terms = {
        "click",
        "computer",
        "cursor",
        "device",
        "keyboard",
        "laptop",
        "screen",
        "usb",
    }
    _animal_terms = {"animal", "cheese", "field", "pet", "rodent", "tail", "wild"}
    _abstract_terms = {
        "belonging",
        "confidence",
        "freedom",
        "hope",
        "identity",
        "patience",
        "resilience",
        "trust",
        "value",
    }
    _action_terms = {
        "build",
        "building",
        "built",
        "carry",
        "click",
        "climb",
        "connect",
        "cross",
        "earn",
        "grow",
        "hold",
        "invest",
        "journey",
        "measure",
        "move",
        "navigate",
        "open",
        "plant",
        "protect",
        "save",
        "share",
        "show",
        "track",
        "trust",
        "use",
        "walk",
        "watch",
        "work",
    }
    _sensitive_terms = {
        "abuse",
        "attack",
        "death",
        "disaster",
        "died",
        "dying",
        "grief",
        "murder",
        "suicide",
        "trauma",
        "violence",
        "war",
    }
    _avoid_term_map = {
        "faces": {"face", "faces"},
        "people's faces": {"face", "faces"},
        "alcohol": {"alcohol", "beer", "wine"},
        "logos": {"brand", "brands", "logo", "logos"},
    }

    def analyze(self, text: str) -> dict[str, object]:
        words = _word_set(text)
        lowered = text.casefold()
        concept, interpretation, rationale = self._interpret(words, lowered)
        if re.search(r"\b\d+(?:\.\d+)?\s*%", text) or re.search(
            r"\b(?:19|20)\d{2}\b", text
        ):
            if concept == "general narration":
                concept = "reported statistic"
            visual_intent = (
                "A clearly contextualized data visualization or timeline; preserve the stated "
                "number and year without implying independent verification."
            )
            rationale.append("Detected a numeric percentage or year; treat numbers as narrated claims.")
        elif concept == "abstract trust":
            visual_intent = (
                "Show reliability accumulating through repeated small actions; use a bridge, "
                "carefully joined pieces, or two people keeping a commitment, not a literal trust document."
            )
        elif concept == "financial value":
            visual_intent = (
                "Show a considered purchase decision, useful product comparison, or household "
                "budget context; avoid cash imagery unless the narration calls for it."
            )
        elif concept == "abstract value":
            visual_intent = (
                "Use human-scale objects or relationships with clear symbolic weight; do not "
                "substitute piles of money for non-financial worth."
            )
        elif concept == "cloud computing":
            visual_intent = (
                "Show data moving between devices and remote servers or a software dashboard; "
                "do not use sky/cloudscape stock imagery."
            )
        elif concept == "weather cloud":
            visual_intent = (
                "Show clouds in an identifiable weather context such as changing sky, rain, "
                "forecast, or landscape."
            )
        elif concept == "computer mouse":
            visual_intent = "Show a computer pointing device in use beside a screen or keyboard."
        elif concept == "animal mouse":
            visual_intent = "Show a small rodent in a natural or domestic animal context; no computer props."
        elif concept == "river bank":
            visual_intent = "Show the edge of a river, waterline, shoreline vegetation, or flowing water."
        elif concept == "financial bank":
            visual_intent = "Show a financial service/account context, not a river or shoreline."
        else:
            visual_intent = _fallback_visual_intent(text, concept)

        entities = _entities(text)
        actions = [word for word in _words(text) if word.casefold() in self._action_terms]
        sensitive_matches = sorted(words & self._sensitive_terms)
        if sensitive_matches:
            mood = "sensitive"
            sensitivity = "high"
            rationale.append("Sensitive-topic terms detected; prefer respectful, non-graphic visuals.")
        elif any(word in words for word in {"sad", "loss", "memorial", "mourning"}):
            mood = "somber"
            sensitivity = "medium"
        elif any(word in words for word in {"celebrate", "joy", "happy", "hope"}):
            mood = "warm and hopeful"
            sensitivity = "low"
        elif any(word in words for word in {"urgent", "warning", "risk", "danger"}):
            mood = "cautious"
            sensitivity = "medium"
        else:
            mood = "neutral"
            sensitivity = "low"

        avoid_constraints = []
        for constraint, terms in self._avoid_term_map.items():
            if words & terms:
                avoid_constraints.append(constraint)
        avoid_constraints.extend(_normalized_avoid_hints(text))
        shot_type = (
            "close-up detail"
            if any(token in words for token in {"device", "click", "tool", "keyboard"})
            else "wide contextual shot"
            if any(token in words for token in {"landscape", "river", "city", "journey"})
            else "medium observational shot"
        )
        composition = (
            "Keep the primary subject in the central safe area; reserve space for captions."
        )
        if concept in {"cloud computing", "reported statistic"}:
            composition = "Use a clean, legible screen or diagram with protected caption space."
        return {
            "central_concept": concept,
            "interpretation": interpretation,
            "intent": visual_intent,
            "entities": entities,
            "actions": actions,
            "abstract_ideas": sorted(words & self._abstract_terms),
            "mood": mood,
            "sensitivity": sensitivity,
            "sensitive_terms": sensitive_matches,
            "avoid_constraints": sorted(set(avoid_constraints)),
            "desired_shot_type": shot_type,
            "composition": composition,
            "visual_continuity": "Natural-light observational B-roll with consistent tonal warmth.",
            "semantic_evidence": rationale,
            "selection_method": "deterministic_context_rules",
            "fallback_reason": None,
        }

    def _interpret(self, words: set[str], lowered: str) -> tuple[str, str, list[str]]:
        rationale: list[str] = []
        cloud_terms = words & {"cloud", "clouds"}
        if "cloud computing" in lowered or (
            cloud_terms and bool(words & self._computing_terms)
        ):
            rationale.append("Cloud term is grounded by computing/data/software context.")
            return "cloud computing", "Remote computing/data services", rationale
        if cloud_terms and words & self._sky_terms:
            rationale.append("Cloud term is grounded by weather or sky context.")
            return "weather cloud", "Cloud as a weather phenomenon", rationale
        if "value" in words and words & self._finance_terms:
            rationale.append("Value term co-occurs with financial or purchase context.")
            return "financial value", "Monetary, purchase, or savings value", rationale
        if "value" in words and words & self._abstract_value_terms:
            rationale.append("Value term co-occurs with trust, meaning, or relationship context.")
            return "abstract value", "Non-monetary human or relational worth", rationale
        if "trust" in words:
            rationale.append("Trust is treated as an abstract quality unless a specific object is named.")
            return "abstract trust", "Confidence built through reliable actions", rationale
        if "mouse" in words and words & self._device_terms:
            rationale.append("Mouse term co-occurs with computer-device context.")
            return "computer mouse", "Computer pointing device", rationale
        if "mouse" in words and words & self._animal_terms:
            rationale.append("Mouse term co-occurs with animal or habitat context.")
            return "animal mouse", "Small rodent in its habitat", rationale
        if "mouse" in words:
            rationale.append("Mouse meaning is ambiguous; a neutral animal/device visual is safer.")
            return "ambiguous mouse", "Meaning unresolved without further context", rationale
        if "bank" in words and words & (self._river_terms - {"bank"}):
            if words & {"account", "deposit", "loan", "money", "financial"}:
                rationale.append("Bank context includes financial-service vocabulary.")
                return "financial bank", "Financial institution", rationale
            rationale.append("Bank context includes river, water, or shoreline vocabulary.")
            return "river bank", "River edge or shoreline", rationale
        if "bank" in words and words & self._finance_terms:
            rationale.append("Bank context includes account, loan, or money vocabulary.")
            return "financial bank", "Financial institution", rationale
        if "bank" in words:
            rationale.append("Bank meaning is ambiguous; avoid an ungrounded institution or river assumption.")
            return "ambiguous bank", "Meaning unresolved without further context", rationale
        if cloud_terms:
            rationale.append("Cloud meaning is ambiguous without computing or weather context.")
            return "ambiguous cloud", "Meaning unresolved without further context", rationale
        if words & self._abstract_terms:
            abstract_term = sorted(words & self._abstract_terms)[0]
            rationale.append(f"{abstract_term.title()} is treated as an abstract concept.")
            return f"abstract {abstract_term}", f"An idea rather than a literal object", rationale
        return "general narration", "Contextual visual representation of the narration", rationale


@dataclass(frozen=True)
class PlannerSettings:
    min_scene_seconds: float = 1.5
    max_scene_seconds: float = 12.0
    boundary_tolerance_seconds: float = 0.05


class ScenePlanningError(ValueError):
    pass


def normalize_script(script: str) -> tuple[str, str]:
    original = script
    normalized = unicodedata.normalize("NFC", script)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        raise ScenePlanningError("Script must contain non-whitespace narration.")
    return original, normalized


def build_scene_plan(
    *,
    transcript: str,
    transcript_segments: tuple[TranscriptSegment, ...] | list[TranscriptSegment],
    duration_seconds: float,
    source_type: str,
    semantic_provider: SemanticProvider | None = None,
    structured_scenes: list[dict[str, object]] | None = None,
    visual_style: str = "",
    avoid: list[str] | None = None,
    settings: PlannerSettings | None = None,
) -> dict[str, object]:
    if duration_seconds <= 0:
        raise ScenePlanningError("Audio duration must be positive.")
    if not transcript.strip():
        raise ScenePlanningError("Transcript must not be empty.")
    rules = settings or PlannerSettings()
    if rules.min_scene_seconds > rules.max_scene_seconds:
        raise ScenePlanningError("Minimum scene duration must not exceed the maximum duration.")
    normalized_transcript = unicodedata.normalize("NFC", transcript)
    provider = semantic_provider or DeterministicSemanticProvider()
    narration_segments = tuple(transcript_segments)
    fallback_reasons: list[str] = []
    if not narration_segments:
        narration_segments = _estimated_segments(normalized_transcript, duration_seconds)
        fallback_reasons.append(
            "No word/phrase timestamps were supplied; word timing was estimated by token length "
            "and normalized across the measured narration duration."
        )
    expanded_segments = _expand_timed_segments(narration_segments, rules.max_scene_seconds)
    if len(expanded_segments) != len(narration_segments):
        fallback_reasons.append(
            "Recognition timestamps covered multiword phrases; internal word times were apportioned "
            "by token length to recover punctuation boundaries and split overlong phrases."
        )
    narration_segments = expanded_segments
    _validate_segments(narration_segments, duration_seconds, rules.boundary_tolerance_seconds)

    if structured_scenes is None:
        groups = _natural_groups(narration_segments, rules)
        scene_sources = [
            {
                "text": " ".join(segment.text for segment in group),
                "visual_hint": None,
                "source_segment_indices": [
                    segment_index
                    for segment_index, segment in enumerate(narration_segments)
                    if segment in group
                ],
            }
            for group in groups
        ]
        timing_sources = groups
    else:
        if not structured_scenes:
            raise ScenePlanningError("Structured scenes must contain at least one scene.")
        scene_sources, timing_sources = _align_structured_scenes(
            structured_scenes, narration_segments, duration_seconds
        )
        fallback_reasons.append(
            "Structured scenes are preserved in supplied order; scene timing follows the measured "
            "narration duration and token-weighted text length."
        )

    scene_records = []
    cursor = 0.0
    warnings: list[str] = []
    for index, (scene_source, timed_segments) in enumerate(zip(scene_sources, timing_sources), start=1):
        text = str(scene_source["text"])
        if not text.strip():
            raise ScenePlanningError(f"Scene {index} has no narration text.")
        start = (
            float(scene_source["start_seconds"])
            if "start_seconds" in scene_source
            else 0.0
            if index == 1
            else cursor
        )
        end = (
            float(scene_source["end_seconds"])
            if "end_seconds" in scene_source
            else duration_seconds
            if index == len(scene_sources)
            else _scene_end(timed_segments, start, duration_seconds)
        )
        end = min(duration_seconds, max(start, end))
        if end - start < rules.boundary_tolerance_seconds:
            raise ScenePlanningError(
                f"Scene {index} cannot be assigned positive duration within the boundary tolerance."
            )
        try:
            analysis = provider.analyze(text)
            if not isinstance(analysis, dict) or not analysis.get("central_concept"):
                raise ValueError("Semantic provider returned an incomplete analysis.")
            analysis = dict(analysis)
            analysis.setdefault("selection_method", "configured_semantic_provider")
            analysis.setdefault("fallback_reason", None)
        except Exception as exc:
            analysis = DeterministicSemanticProvider().analyze(text)
            reason = (
                f"Semantic provider {type(provider).__name__} failed with "
                f"{type(exc).__name__}; deterministic context rules were used."
            )
            analysis["selection_method"] = "deterministic_fallback"
            analysis["fallback_reason"] = reason
            fallback_reasons.append(f"Scene {index}: {reason}")
            warnings.append(f"Scene {index}: semantic provider fallback was applied.")

        hint = scene_source.get("visual_hint")
        hint_assessment = _assess_hint(str(hint) if hint else None, analysis)
        if hint and hint_assessment["alignment"] == "potential_conflict":
            warnings.append(
                f"Scene {index}: visual_hint was retained as optional guidance but did not replace "
                "the context-derived central concept."
            )
        analysis["visual_hint"] = hint
        analysis["visual_hint_assessment"] = hint_assessment
        analysis["source_segment_indices"] = scene_source["source_segment_indices"]
        analysis["visual_style"] = visual_style or None
        explicit_avoid = list(avoid or [])
        analysis["avoid_constraints"] = sorted(
            set(analysis.get("avoid_constraints", [])) | set(explicit_avoid)
        )
        analysis["sentence_or_phrase_boundary"] = _boundary_description(
            timed_segments, rules.min_scene_seconds
        )
        scene_records.append(
            {
                "scene_id": f"scene_{index:03d}",
                "order": index,
                "narration_text": text,
                "start_seconds": round(start, 6),
                "end_seconds": round(end, 6),
                "duration_seconds": round(end - start, 6),
                "intent_graph": analysis,
            }
        )
        cursor = end

    coverage = _validate_coverage(
        scene_records,
        duration_seconds,
        rules.boundary_tolerance_seconds,
    )
    return {
        "schema_version": "1.0",
        "source_type": source_type,
        "timing_authority": "measured_generated_audio" if source_type in {"script", "scenes"} else "source_audio",
        "duration_seconds": round(duration_seconds, 6),
        "coverage": coverage,
        "transcript": {
            "original_text": transcript,
            "normalized_text": normalized_transcript,
            "segments": [
                {
                    "text": segment.text,
                    "start_seconds": round(segment.start_seconds, 6),
                    "end_seconds": round(segment.end_seconds, 6),
                    "confidence": segment.confidence,
                    "timing_source": segment.timing_source,
                }
                for segment in narration_segments
            ],
        },
        "scenes": scene_records,
        "fallback_reasons": fallback_reasons,
        "warnings": warnings,
    }


def _natural_groups(
    segments: tuple[TranscriptSegment, ...],
    settings: PlannerSettings,
) -> list[list[TranscriptSegment]]:
    phrases: list[list[TranscriptSegment]] = []
    current: list[TranscriptSegment] = []
    for index, segment in enumerate(segments):
        if current and segment.start_seconds - current[-1].end_seconds > 0.75:
            phrases.append(current)
            current = []
        current.append(segment)
        gap_boundary = (
            index + 1 < len(segments)
            and segments[index + 1].start_seconds - segment.end_seconds > 0.45
        )
        punctuation_boundary = bool(re.search(r"[.!?;:]$", segment.text))
        current_duration = current[-1].end_seconds - current[0].start_seconds
        if punctuation_boundary or gap_boundary or current_duration >= settings.max_scene_seconds:
            phrases.append(current)
            current = []
    if current:
        phrases.append(current)

    merged: list[list[TranscriptSegment]] = []
    for phrase in phrases:
        if not merged:
            merged.append(phrase)
            continue
        previous_duration = merged[-1][-1].end_seconds - merged[-1][0].start_seconds
        phrase_duration = phrase[-1].end_seconds - phrase[0].start_seconds
        phrase_has_boundary = bool(re.search(r"[.!?;:]$", merged[-1][-1].text))
        if (
            previous_duration < settings.min_scene_seconds
            and previous_duration + phrase_duration <= settings.max_scene_seconds
        ):
            merged[-1].extend(phrase)
            if not phrase_has_boundary:
                continue
        else:
            merged.append(phrase)
    return merged


def _align_structured_scenes(
    scenes: list[dict[str, object]],
    transcript_segments: tuple[TranscriptSegment, ...],
    duration: float,
) -> tuple[list[dict[str, object]], list[tuple[TranscriptSegment, ...]]]:
    normalized_scenes = []
    for index, scene in enumerate(scenes):
        text = unicodedata.normalize("NFC", str(scene.get("text", ""))).strip()
        if not text:
            raise ScenePlanningError(f"Structured scene {index + 1} must contain narration text.")
        normalized_scenes.append(
            {
                "text": text,
                "visual_hint": scene.get("visual_hint"),
                "source_segment_indices": [],
            }
        )

    weight = [max(1, len(str(scene["text"]).split())) for scene in normalized_scenes]
    total_weight = sum(weight)
    cursor = 0.0
    timed_groups = []
    for index, (scene, scene_weight) in enumerate(zip(normalized_scenes, weight)):
        end = duration if index == len(normalized_scenes) - 1 else cursor + duration * scene_weight / total_weight
        scene["start_seconds"] = cursor
        scene["end_seconds"] = end
        relevant = tuple(
            segment
            for segment in transcript_segments
            if segment.end_seconds > cursor and segment.start_seconds < end
        )
        scene["source_segment_indices"] = [
            segment_index
            for segment_index, segment in enumerate(transcript_segments)
            if segment in relevant
        ]
        timed_groups.append(relevant)
        cursor = end
    return normalized_scenes, timed_groups


def _scene_end(
    segments: tuple[TranscriptSegment, ...] | list[TranscriptSegment],
    start: float,
    duration: float,
) -> float:
    if not segments:
        return min(duration, start)
    return min(duration, max(start, segments[-1].end_seconds))


def _validate_segments(
    segments: tuple[TranscriptSegment, ...],
    duration: float,
    tolerance: float,
) -> None:
    previous_start = -tolerance
    for index, segment in enumerate(segments):
        if not segment.text.strip():
            raise ScenePlanningError(f"Transcript segment {index + 1} is empty.")
        if segment.start_seconds < -tolerance or segment.end_seconds > duration + tolerance:
            raise ScenePlanningError(f"Transcript segment {index + 1} is outside the audio duration.")
        if segment.end_seconds < segment.start_seconds:
            raise ScenePlanningError(f"Transcript segment {index + 1} has an invalid time interval.")
        if segment.start_seconds < previous_start - tolerance:
            raise ScenePlanningError("Transcript segments must be ordered by start time.")
        if segment.confidence is not None and not 0 <= segment.confidence <= 1:
            raise ScenePlanningError(f"Transcript segment {index + 1} has invalid confidence.")
        previous_start = segment.start_seconds


def _estimated_segments(text: str, duration: float) -> tuple[TranscriptSegment, ...]:
    words = text.split()
    if not words:
        return ()
    weights = [max(1, len(word.strip(".,!?;:"))) for word in words]
    total_weight = sum(weights)
    cursor = 0.0
    result = []
    for index, (word, weight) in enumerate(zip(words, weights)):
        end = duration if index == len(words) - 1 else cursor + duration * weight / total_weight
        result.append(TranscriptSegment(word, cursor, end, timing_source="estimated_from_text"))
        cursor = end
    return tuple(result)


def _expand_timed_segments(
    segments: tuple[TranscriptSegment, ...],
    maximum_phrase_seconds: float,
) -> tuple[TranscriptSegment, ...]:
    expanded = []
    for segment in segments:
        words = segment.text.split()
        duration = segment.end_seconds - segment.start_seconds
        if len(words) <= 1 or (
            duration <= maximum_phrase_seconds
            and not any(re.search(r"[.!?;:]$", word) for word in words[:-1])
        ):
            expanded.append(segment)
            continue
        weights = [max(1, len(word.strip(".,!?;:"))) for word in words]
        total_weight = sum(weights)
        cursor = segment.start_seconds
        for index, (word, weight) in enumerate(zip(words, weights)):
            end = (
                segment.end_seconds
                if index == len(words) - 1
                else cursor + duration * weight / total_weight
            )
            expanded.append(
                TranscriptSegment(
                    text=word,
                    start_seconds=cursor,
                    end_seconds=end,
                    confidence=segment.confidence,
                    timing_source="estimated_within_recognized_phrase",
                )
            )
            cursor = end
    return tuple(expanded)


def _validate_coverage(scenes: list[dict[str, object]], duration: float, tolerance: float) -> dict:
    if not scenes:
        raise ScenePlanningError("A scene plan must contain at least one scene.")
    if abs(float(scenes[0]["start_seconds"])) > tolerance:
        raise ScenePlanningError("Scene plan does not begin at the narration start.")
    gaps = []
    overlaps = []
    for index, scene in enumerate(scenes):
        start = float(scene["start_seconds"])
        end = float(scene["end_seconds"])
        if end <= start:
            raise ScenePlanningError(f"Scene {index + 1} must have positive duration.")
        if index:
            difference = start - float(scenes[index - 1]["end_seconds"])
            if difference > tolerance:
                gaps.append({"after_scene": index, "gap_seconds": round(difference, 6)})
            elif difference < -tolerance:
                overlaps.append({"before_scene": index + 1, "overlap_seconds": round(-difference, 6)})
    tail_gap = duration - float(scenes[-1]["end_seconds"])
    if tail_gap > tolerance:
        gaps.append({"after_scene": len(scenes), "gap_seconds": round(tail_gap, 6)})
    if gaps or overlaps:
        raise ScenePlanningError(
            f"Scene plan has gaps or overlaps beyond the {tolerance:.3f}s tolerance."
        )
    if abs(float(scenes[-1]["end_seconds"]) - duration) > tolerance:
        raise ScenePlanningError("Scene plan does not cover the full narration duration.")
    return {
        "covered_seconds": round(duration, 6),
        "coverage_ratio": 1.0,
        "maximum_gap_seconds": 0.0,
        "maximum_overlap_seconds": 0.0,
        "boundary_tolerance_seconds": tolerance,
        "gap_count": 0,
        "overlap_count": 0,
    }


def _assess_hint(hint: str | None, analysis: dict[str, object]) -> dict[str, str | None]:
    if not hint or not hint.strip():
        return {"alignment": "not_provided", "reason": "No visual hint was supplied."}
    hint_words = _word_set(hint)
    concept_words = _word_set(str(analysis["central_concept"]))
    intent_words = _word_set(str(analysis["intent"]))
    concept = str(analysis["central_concept"])
    term_pairs = {
        ("server", "sky"),
        ("computing", "weather"),
        ("river", "account"),
        ("rodent", "computer"),
        ("cash", "trust"),
    }
    contradiction = (
        concept == "cloud computing" and bool(hint_words & DeterministicSemanticProvider._sky_terms)
    ) or (
        concept == "weather cloud"
        and bool(hint_words & DeterministicSemanticProvider._computing_terms)
    ) or (
        concept == "financial value" and bool(hint_words & {"trust", "relationship", "meaning"})
    ) or (
        concept == "abstract trust" and bool(hint_words & {"cash", "money", "coins"})
    ) or (
        concept == "computer mouse" and bool(hint_words & DeterministicSemanticProvider._animal_terms)
    ) or (
        concept == "animal mouse" and bool(hint_words & DeterministicSemanticProvider._device_terms)
    ) or (
        concept == "financial bank" and bool(hint_words & {"river", "shore", "water", "stream"})
    ) or (
        concept == "river bank" and bool(hint_words & {"account", "deposit", "loan", "money"})
    ) or any(
        first in hint_words and second in concept_words | intent_words
        or second in hint_words and first in concept_words | intent_words
        for first, second in term_pairs
    )
    if contradiction:
        return {
            "alignment": "potential_conflict",
            "reason": "The hint may conflict with contextual meaning and is not allowed to replace it.",
        }
    return {
        "alignment": "refines",
        "reason": "The hint adds visual direction while the context-derived central concept is retained.",
    }


def _boundary_description(
    segments: tuple[TranscriptSegment, ...] | list[TranscriptSegment],
    minimum: float,
) -> str:
    if not segments:
        return "Estimated from narration text; no recognition timestamps were available."
    if segments[-1].end_seconds - segments[0].start_seconds < minimum:
        return "Short phrase retained and merged where possible to avoid a flashing scene."
    if re.search(r"[.!?;:]$", segments[-1].text):
        return "Sentence or clause punctuation boundary."
    return "Phrase boundary selected from recognized segment timing."


def _fallback_visual_intent(text: str, concept: str) -> str:
    if re.search(r"\b(?:because|therefore|result|leads to|causes)\b", text, re.IGNORECASE):
        return "Show the cause-and-effect relationship using a grounded, non-literal transition."
    return f"Show a contextually grounded visual for {concept}; avoid an unsupported literal assumption."


def _word_set(text: str) -> set[str]:
    return {word.casefold() for word in re.findall(r"[\w']+", text, flags=re.UNICODE)}


def _words(text: str) -> list[str]:
    return re.findall(r"[\w']+", text, flags=re.UNICODE)


def _entities(text: str) -> list[str]:
    entities = []
    for match in re.finditer(r"\b[A-Z][\w'-]*(?:\s+[A-Z][\w'-]*)*\b", text):
        entity = match.group().strip()
        if entity.casefold() not in {"i", "a", "the", "in", "on", "at", "we", "it"}:
            entities.append(entity)
    return list(dict.fromkeys(entities))


def _normalized_avoid_hints(text: str) -> list[str]:
    constraints = []
    if re.search(r"\bwithout\s+(?:showing\s+)?(?:faces|logos|money)\b", text, re.I):
        match = re.search(r"\bwithout\s+(?:showing\s+)?([\w ]+)", text, re.I)
        if match:
            constraints.append(match.group(1).strip().casefold())
    return constraints
