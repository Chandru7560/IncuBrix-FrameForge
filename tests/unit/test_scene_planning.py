from __future__ import annotations

import pytest

from scenepilot.providers.narration import TranscriptSegment
from scenepilot.services.narrative_intent_graph import (
    DeterministicSemanticProvider,
    PlannerSettings,
    ScenePlanningError,
    build_scene_plan,
    normalize_script,
)


@pytest.mark.parametrize(
    ("text", "expected_concept"),
    [
        ("Cloud computing moves data between servers.", "cloud computing"),
        ("Dark clouds brought rain across the sky.", "weather cloud"),
        ("The value of a lower price helps families save money.", "financial value"),
        ("The value of trust matters more than a number.", "abstract value"),
        ("A mouse clicks beside the computer keyboard.", "computer mouse"),
        ("A mouse searches for cheese in the field.", "animal mouse"),
        ("The bank approved a loan for the business.", "financial bank"),
        ("The river bank eroded after heavy rain.", "river bank"),
        ("Trust is earned slowly through repeated small promises.", "abstract trust"),
        ("Sales grew 40% in 2025.", "reported statistic"),
    ],
)
def test_contextual_concepts_and_visual_intents(text, expected_concept):
    plan = build_scene_plan(
        transcript=text,
        transcript_segments=[TranscriptSegment(text, 0, 8)],
        duration_seconds=8,
        source_type="audio_upload",
    )
    scene = plan["scenes"][0]
    intent = scene["intent_graph"]
    assert intent["central_concept"] == expected_concept
    assert intent["intent"]
    assert intent["selection_method"] == "deterministic_context_rules"


def test_cloud_hint_does_not_override_cloud_computing_meaning():
    text = "Cloud computing moves data between remote servers."
    plan = build_scene_plan(
        transcript=text,
        transcript_segments=[TranscriptSegment(text, 0, 8)],
        duration_seconds=8,
        source_type="scenes",
        structured_scenes=[
            {"text": text, "visual_hint": "Photograph blue clouds in the sky"}
        ],
    )
    intent = plan["scenes"][0]["intent_graph"]
    assert intent["central_concept"] == "cloud computing"
    assert intent["visual_hint_assessment"]["alignment"] == "potential_conflict"
    assert "did not replace" in " ".join(plan["warnings"])


def test_abstract_ideas_actions_entities_mood_and_avoid_constraints():
    text = "Trust is built patiently. Protect the promise."
    plan = build_scene_plan(
        transcript=text,
        transcript_segments=[
            TranscriptSegment("Trust is built patiently.", 0, 3),
            TranscriptSegment("Protect the promise.", 3, 6),
        ],
        duration_seconds=6,
        source_type="audio_upload",
        avoid=["faces", "alcohol"],
    )
    intent = plan["scenes"][0]["intent_graph"]
    assert "trust" in intent["abstract_ideas"]
    assert "built" in intent["actions"]
    assert {"faces", "alcohol"} <= set(intent["avoid_constraints"])
    assert intent["sensitivity"] == "low"


def test_short_phrases_merge_and_long_narration_splits_at_maximum():
    short_plan = build_scene_plan(
        transcript="Trust grows.",
        transcript_segments=[
            TranscriptSegment("Trust", 0, 0.4),
            TranscriptSegment("grows.", 0.4, 0.8),
        ],
        duration_seconds=0.8,
        source_type="audio_upload",
        settings=PlannerSettings(min_scene_seconds=1.5, max_scene_seconds=8),
    )
    assert len(short_plan["scenes"]) == 1

    long_words = [f"word{index}" for index in range(50)]
    long_text = " ".join(long_words)
    long_segments = [
        TranscriptSegment(word, index * 0.4, (index + 1) * 0.4)
        for index, word in enumerate(long_words)
    ]
    long_plan = build_scene_plan(
        transcript=long_text,
        transcript_segments=long_segments,
        duration_seconds=20,
        source_type="audio_upload",
        settings=PlannerSettings(min_scene_seconds=1.5, max_scene_seconds=6),
    )
    assert len(long_plan["scenes"]) >= 4
    assert all(scene["duration_seconds"] <= 6.000001 for scene in long_plan["scenes"])


def test_scene_timeline_has_full_contiguous_coverage_with_documented_tolerance():
    plan = build_scene_plan(
        transcript="First phrase. Second phrase.",
        transcript_segments=[
            TranscriptSegment("First", 0, 1),
            TranscriptSegment("phrase.", 1, 2),
            TranscriptSegment("Second", 2.4, 4),
            TranscriptSegment("phrase.", 4, 5),
        ],
        duration_seconds=6,
        source_type="audio_upload",
    )
    scenes = plan["scenes"]
    assert scenes[0]["start_seconds"] == 0
    assert scenes[-1]["end_seconds"] == 6
    assert all(
        current["start_seconds"] == previous["end_seconds"]
        for previous, current in zip(scenes, scenes[1:])
    )
    assert plan["coverage"]["coverage_ratio"] == 1.0
    assert plan["coverage"]["boundary_tolerance_seconds"] == 0.05


def test_invalid_transcript_timestamps_are_rejected():
    with pytest.raises(ScenePlanningError, match="outside the audio duration"):
        build_scene_plan(
            transcript="Out of bounds.",
            transcript_segments=[TranscriptSegment("Out", 0, 11)],
            duration_seconds=10,
            source_type="audio_upload",
        )


def test_semantic_provider_failure_uses_recorded_deterministic_fallback():
    class FailedSemanticProvider:
        def analyze(self, text):
            raise TimeoutError("test timeout")

    plan = build_scene_plan(
        transcript="Trust is earned.",
        transcript_segments=[TranscriptSegment("Trust is earned.", 0, 3)],
        duration_seconds=3,
        source_type="audio_upload",
        semantic_provider=FailedSemanticProvider(),
    )
    assert plan["scenes"][0]["intent_graph"]["central_concept"] == "abstract trust"
    assert plan["scenes"][0]["intent_graph"]["selection_method"] == "deterministic_fallback"
    assert "TimeoutError" in plan["fallback_reasons"][0]


def test_script_normalization_preserves_original_and_normalizes_spacing():
    original, normalized = normalize_script("  Trust\tis earned.\n Slowly.  ")
    assert original == "  Trust\tis earned.\n Slowly.  "
    assert normalized == "Trust is earned. Slowly."

