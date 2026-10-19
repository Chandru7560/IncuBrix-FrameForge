"""Explainable visual candidate retrieval, filtering, ranking, and rights passports."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from scenepilot.config import Settings
from scenepilot.providers.footage import (
    DeterministicLocalFootageAdapter,
    FootageCandidate,
    FootageProviderError,
    FootageSearchResult,
    FootageSourceAdapter,
    PexelsVideoAdapter,
    PixabayVideoAdapter,
)
from scenepilot.services.footage_download import (
    DownloadedFootage,
    FootageAssetDownloadError,
    download_and_validate_footage,
)

logger = logging.getLogger("scenepilot.visual_retrieval")

_ASPECT_RATIOS = {"16:9": 16 / 9, "9:16": 9 / 16, "1:1": 1.0}
_STOPWORDS = {
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "is", "of",
    "on", "or", "show", "the", "to", "use", "with",
}


@dataclass(frozen=True)
class VisualRetrievalPolicy:
    source_order: tuple[str, ...]
    minimum_relevance_score: float
    aspect_tolerance: float
    semantic_weight: float
    action_weight: float
    subject_weight: float
    composition_weight: float
    shot_variety_weight: float
    technical_weight: float
    max_candidates_per_source: int

    def weights(self) -> dict[str, float]:
        return {
            "semantic_relevance": self.semantic_weight,
            "action_match": self.action_weight,
            "subject_match": self.subject_weight,
            "composition": self.composition_weight,
            "shot_variety": self.shot_variety_weight,
            "technical_suitability": self.technical_weight,
        }


def policy_from_settings(settings: Settings) -> VisualRetrievalPolicy:
    return VisualRetrievalPolicy(
        source_order=settings.footage_source_order,
        minimum_relevance_score=settings.footage_min_relevance_score,
        aspect_tolerance=settings.footage_aspect_tolerance,
        semantic_weight=settings.footage_semantic_weight,
        action_weight=settings.footage_action_weight,
        subject_weight=settings.footage_subject_weight,
        composition_weight=settings.footage_composition_weight,
        shot_variety_weight=settings.footage_shot_variety_weight,
        technical_weight=settings.footage_technical_weight,
        max_candidates_per_source=settings.max_footage_candidates_per_source,
    )


def configured_footage_sources(settings: Settings) -> tuple[FootageSourceAdapter, ...]:
    sources: dict[str, FootageSourceAdapter] = {
        "local": DeterministicLocalFootageAdapter()
    }
    if settings.pexels_api_key:
        sources["pexels"] = PexelsVideoAdapter(
            settings.pexels_api_key,
            rights_policy_approved=settings.pexels_rights_policy_approved,
        )
    if settings.pixabay_api_key:
        sources["pixabay"] = PixabayVideoAdapter(
            settings.pixabay_api_key,
            rights_policy_approved=settings.pixabay_rights_policy_approved,
        )
    return tuple(sources[name] for name in settings.footage_source_order if name in sources)


def build_visual_search_description(
    intent: dict[str, object],
    narration_text: str,
    *,
    visual_style: str = "",
) -> str:
    concept = str(intent.get("central_concept") or "narration scene")
    visual_intent = str(intent.get("intent") or narration_text)
    entities = ", ".join(str(item) for item in intent.get("entities", []))
    actions = ", ".join(str(item) for item in intent.get("actions", []))
    parts = [
        visual_intent,
        f"Concept: {concept}.",
        f"Scene context: {narration_text}",
    ]
    hint = intent.get("visual_hint")
    assessment = intent.get("visual_hint_assessment")
    if hint and not (
        isinstance(assessment, dict)
        and assessment.get("alignment") == "potential_conflict"
    ):
        parts.append(f"Visual guidance: {hint}.")
    if entities:
        parts.append(f"Subjects: {entities}.")
    if actions:
        parts.append(f"Actions: {actions}.")
    if visual_style.strip():
        parts.append(f"Style: {visual_style.strip()}.")
    return re.sub(r"\s+", " ", " ".join(parts)).strip()[:500]


class VisualRetrievalService:
    def __init__(
        self,
        settings: Settings,
        *,
        sources: tuple[FootageSourceAdapter, ...] | None = None,
        downloader: Callable[[FootageCandidate], DownloadedFootage] | None = None,
    ) -> None:
        self.settings = settings
        self.policy = policy_from_settings(settings)
        self.sources = sources if sources is not None else configured_footage_sources(settings)
        self.downloader = downloader or self._download

    def select(
        self,
        scene: dict[str, object],
        *,
        aspect_ratio: str,
        avoid: list[str],
        selected_asset_ids: set[str],
        previous_shot_type: str | None = None,
        visual_style: str = "",
    ) -> dict[str, object]:
        if aspect_ratio not in _ASPECT_RATIOS:
            raise ValueError(f"Unsupported output aspect ratio: {aspect_ratio}")
        intent = scene.get("intent_graph")
        if not isinstance(intent, dict):
            raise ValueError("Scene plan is missing its intent graph.")
        narration_text = str(scene.get("narration_text") or "")
        query = build_visual_search_description(
            intent,
            narration_text,
            visual_style=visual_style,
        )
        duration = float(scene.get("duration_seconds") or 0)
        rejected: list[dict[str, object]] = []
        attempts: list[dict[str, object]] = []

        for source in self._ordered_sources():
            try:
                result = source.search(query, limit=self.policy.max_candidates_per_source)
                if result.provider != source.name:
                    raise FootageProviderError(
                        source.name,
                        "Provider response identity did not match the configured source.",
                        retryable=False,
                    )
            except Exception as exc:
                attempts.append(
                    {
                        "provider": source.name,
                        "outcome": "unavailable",
                        "retryable": getattr(exc, "retryable", False),
                        "error_type": type(exc).__name__,
                        "message": str(exc) if isinstance(exc, FootageProviderError) else (
                            "Provider adapter failed; see sanitized logs."
                        ),
                    }
                )
                logger.warning(
                    "footage_provider_failed",
                    extra={
                        "provider": source.name,
                        "error_type": type(exc).__name__,
                        "retryable": getattr(exc, "retryable", False),
                    },
                )
                continue

            attempt = {
                "provider": source.name,
                "outcome": "empty_results" if not result.candidates else "evaluated",
                "candidate_count": len(result.candidates),
                "response_metadata": result.response_metadata,
            }
            attempts.append(attempt)
            if not result.candidates:
                continue

            evaluated = [
                self._evaluate(
                    candidate,
                    scene=scene,
                    narration_text=narration_text,
                    intent=intent,
                    query=query,
                    aspect_ratio=aspect_ratio,
                    duration=duration,
                    avoid=avoid,
                    selected_asset_ids=selected_asset_ids,
                    previous_shot_type=previous_shot_type,
                )
                for candidate in result.candidates[: self.policy.max_candidates_per_source]
            ]
            eligible = sorted(
                (item for item in evaluated if not item["rejection_reasons"]),
                key=lambda item: float(item["overall_score"]),
                reverse=True,
            )
            rejected.extend(
                self._rejection_record(
                    item["candidate"],
                    item["overall_score"],
                    item["score_components"],
                    item["rationale"],
                    item["checks"],
                    item["rejection_reasons"],
                )
                for item in evaluated
                if item["rejection_reasons"]
            )

            for item in eligible:
                candidate = item["candidate"]
                try:
                    downloaded = self.downloader(candidate)
                except FootageAssetDownloadError as exc:
                    rejected.append(
                        self._rejection_record(
                            candidate,
                            item["overall_score"],
                            item["score_components"],
                            item["rationale"],
                            item["checks"],
                            [f"download:{exc.code}"],
                        )
                    )
                    continue
                except OSError as exc:
                    rejected.append(
                        self._rejection_record(
                            candidate,
                            item["overall_score"],
                            item["score_components"],
                            item["rationale"],
                            item["checks"],
                            [f"download:storage_error:{type(exc).__name__}"],
                        )
                    )
                    continue
                selected_asset_ids.add(f"{candidate.provider}:{candidate.asset_id}")
                attempt["outcome"] = "selected"
                attempt["selected_asset_id"] = candidate.asset_id
                return {
                    "status": "selected",
                    "aspect_ratio": aspect_ratio,
                    "visual_search_description": query,
                    "selection_policy": self._policy_record(),
                    "source_attempts": attempts,
                    "rights_passport": self._rights_passport(
                        candidate,
                        overall_score=item["overall_score"],
                        rationale=item["rationale"],
                        checks=item["checks"],
                        provider_response=candidate.provider_response,
                        downloaded=downloaded,
                    ),
                    "score_components": item["score_components"],
                    "overall_score": item["overall_score"],
                    "selection_rationale": item["rationale"],
                    "selected_shot_type": getattr(candidate, "shot_type", None),
                    "rejected_alternatives": rejected
                    + [
                        self._rejection_record(
                            lower["candidate"],
                            lower["overall_score"],
                            lower["score_components"],
                            lower["rationale"],
                            lower["checks"],
                            ["lower_ranked_than_selected_candidate"],
                        )
                        for lower in eligible
                        if lower is not item
                    ],
                }
            attempt["outcome"] = (
                "no_eligible_candidates" if evaluated else "empty_results"
            )

        return {
            "status": "no_eligible_asset",
            "aspect_ratio": aspect_ratio,
            "visual_search_description": query,
            "selection_policy": self._policy_record(),
            "source_attempts": attempts,
            "rights_passport": None,
            "score_components": None,
            "overall_score": None,
            "selection_rationale": [
                "No candidate passed relevance, technical, safety, duplication, download, and rights checks."
            ],
            "selected_shot_type": None,
            "rejected_alternatives": rejected,
        }

    def _evaluate(
        self,
        candidate: FootageCandidate,
        *,
        scene: dict[str, object],
        narration_text: str,
        intent: dict[str, object],
        query: str,
        aspect_ratio: str,
        duration: float,
        avoid: list[str],
        selected_asset_ids: set[str],
        previous_shot_type: str | None,
    ) -> dict[str, object]:
        score_components = self._scores(
            candidate,
            narration_text=narration_text,
            intent=intent,
            query=query,
            aspect_ratio=aspect_ratio,
            duration=duration,
            previous_shot_type=previous_shot_type,
        )
        overall = round(
            sum(
                score_components[key] * weight
                for key, weight in self.policy.weights().items()
            ),
            4,
        )
        reasons = []
        checks = {
            "commercial_rights": candidate.commercial_use_status == "verified"
            and bool(candidate.licence_name)
            and _is_https_url(candidate.licence_evidence_url)
            and bool(candidate.commercial_use_verified_at),
            "watermark": candidate.watermarked is False,
            "content_safety": candidate.content_safety_status == "metadata_screened",
            "avoid_rules": [],
        }
        if candidate.test_fixture:
            reasons.append("test_fixture_not_publishable")
        if not checks["commercial_rights"]:
            reasons.append("commercial_rights_unverified_or_incomplete")
        if candidate.ai_generated:
            reasons.append("unconfigured_generative_media_not_eligible")
        if candidate.watermarked is True:
            reasons.append("watermarked_asset")
        elif candidate.watermarked is None:
            reasons.append("watermark_status_unverified")
        if not checks["content_safety"]:
            reasons.append(f"content_safety_{candidate.content_safety_status}")
        asset_key = f"{candidate.provider}:{candidate.asset_id}"
        if asset_key in selected_asset_ids:
            reasons.append("duplicate_asset_already_selected_for_this_output")
        if not _is_https_url(candidate.original_asset_url):
            reasons.append("original_asset_url_missing_or_invalid")
        if not _is_https_url(candidate.download_url):
            reasons.append("download_url_missing_or_invalid")
        target_ratio = _ASPECT_RATIOS[aspect_ratio]
        observed_ratio = candidate.width / candidate.height if candidate.height else 0
        ratio_error = abs(observed_ratio - target_ratio) / target_ratio if target_ratio else 1
        if ratio_error > self.policy.aspect_tolerance:
            reasons.append("aspect_ratio_outside_policy_tolerance")
        if candidate.duration_seconds + 0.05 < duration:
            reasons.append("asset_shorter_than_scene")
        candidate_terms = _tokens(
            " ".join((candidate.title, candidate.description, *candidate.tags))
        )
        avoid_checks = []
        for rule in avoid:
            rule_terms = _tokens(rule)
            matched = bool(rule_terms and rule_terms <= candidate_terms)
            avoid_checks.append(
                {
                    "rule": rule,
                    "passed": not matched,
                    "reason": "matched_candidate_metadata" if matched else "no_metadata_match",
                }
            )
            if matched:
                reasons.append(f"avoid_rule_matched:{rule}")
        checks["avoid_rules"] = avoid_checks
        if overall < self.policy.minimum_relevance_score:
            reasons.append("below_minimum_relevance_threshold")
        rationale = [
            f"{key}={score:.3f} weighted {self.policy.weights()[key]:.2f}"
            for key, score in score_components.items()
        ]
        rationale.append(
            f"overall={overall:.3f}; configured minimum={self.policy.minimum_relevance_score:.3f}"
        )
        return {
            "candidate": candidate,
            "score_components": score_components,
            "overall_score": overall,
            "rejection_reasons": reasons,
            "checks": checks,
            "rationale": rationale,
        }

    def _scores(
        self,
        candidate: FootageCandidate,
        *,
        narration_text: str,
        intent: dict[str, object],
        query: str,
        aspect_ratio: str,
        duration: float,
        previous_shot_type: str | None,
    ) -> dict[str, float]:
        candidate_text = " ".join((candidate.title, candidate.description, *candidate.tags))
        candidate_terms = _tokens(candidate_text)
        semantic = _overlap(_tokens(query), candidate_terms)
        actions = _tokens(" ".join(str(item) for item in intent.get("actions", [])))
        action_score = _overlap(actions, candidate_terms)
        subjects = _tokens(
            " ".join(
                [
                    str(intent.get("central_concept", "")),
                    *(str(item) for item in intent.get("entities", [])),
                    narration_text,
                ]
            )
        )
        subject_score = _overlap(subjects, candidate_terms)
        shot_type = getattr(candidate, "shot_type", None)
        if shot_type:
            desired = _tokens(str(intent.get("desired_shot_type", "")))
            composition = _overlap(desired, _tokens(shot_type))
            variety = (
                0.25
                if previous_shot_type and shot_type == previous_shot_type
                else 1.0
            )
        else:
            composition = 0.5
            variety = 0.5
        target_ratio = _ASPECT_RATIOS[aspect_ratio]
        observed_ratio = candidate.width / candidate.height if candidate.height else 0
        ratio_error = abs(observed_ratio - target_ratio) / target_ratio if target_ratio else 1
        aspect_score = max(0.0, 1.0 - ratio_error / max(self.policy.aspect_tolerance, 0.01))
        resolution_score = min(1.0, candidate.width * candidate.height / (1280 * 720))
        duration_score = min(1.0, duration / max(candidate.duration_seconds, duration, 0.01))
        technical = (aspect_score + resolution_score + duration_score) / 3
        return {
            "semantic_relevance": round(semantic, 4),
            "action_match": round(action_score, 4),
            "subject_match": round(subject_score, 4),
            "composition": round(composition, 4),
            "shot_variety": round(variety, 4),
            "technical_suitability": round(technical, 4),
        }

    def _ordered_sources(self) -> tuple[FootageSourceAdapter, ...]:
        source_map = {source.name: source for source in self.sources}
        return tuple(
            source_map[name]
            for name in self.policy.source_order
            if name in source_map
        )

    def _download(self, candidate: FootageCandidate) -> DownloadedFootage:
        directory = self.settings.footage_directory or (
            self.settings.input_directory / "footage"
        )
        return download_and_validate_footage(
            candidate,
            destination_directory=Path(directory),
            max_bytes=self.settings.max_footage_asset_bytes,
            timeout_seconds=self.settings.footage_download_timeout_seconds,
            ffprobe_binary=self.settings.ffprobe_binary,
        )

    def _policy_record(self) -> dict[str, object]:
        return {
            "version": "stock-first-v1",
            "source_order": list(self.policy.source_order),
            "minimum_relevance_score": self.policy.minimum_relevance_score,
            "aspect_tolerance_relative": self.policy.aspect_tolerance,
            "score_weights": self.policy.weights(),
            "rights_required": "verified",
            "watermarked_assets_allowed": False,
            "generative_fallback_enabled": False,
        }

    @staticmethod
    def _rights_passport(
        candidate: FootageCandidate,
        *,
        overall_score: float,
        rationale: list[str],
        checks: dict[str, object],
        provider_response: dict[str, object],
        downloaded: DownloadedFootage,
    ) -> dict[str, object]:
        return {
            "asset_id": candidate.asset_id,
            "provider": candidate.provider,
            "original_asset_url": candidate.original_asset_url,
            "download_url": candidate.download_url,
            "licence_name": candidate.licence_name,
            "licence_evidence_url": candidate.licence_evidence_url,
            "attribution_required": candidate.attribution_required,
            "attribution_text": candidate.attribution_text,
            "commercial_use_verification_status": candidate.commercial_use_status,
            "commercial_use_verified_at": candidate.commercial_use_verified_at,
            "retrieved_at": candidate.retrieved_at,
            "source_terms_reference": candidate.source_terms_reference,
            "ai_generated": candidate.ai_generated,
            "watermarked": candidate.watermarked,
            "watermark_verification": (
                "detected" if candidate.watermarked else
                "clear" if candidate.watermarked is False else "unverified"
            ),
            "content_safety_status": candidate.content_safety_status,
            "content_safety_and_avoid_checks": checks,
            "relevance_score": overall_score,
            "selection_rationale": rationale,
            "provider_response": provider_response,
            "downloaded_asset_key": downloaded.path.name,
            "downloaded_size_bytes": downloaded.size_bytes,
            "downloaded_sha256": downloaded.sha256,
            "downloaded_width": downloaded.width,
            "downloaded_height": downloaded.height,
        }

    @staticmethod
    def _rejection_record(
        candidate: FootageCandidate,
        overall_score: float,
        score_components: dict[str, float],
        rationale: list[str],
        checks: dict[str, object],
        reasons: list[str],
    ) -> dict[str, object]:
        return {
            "asset_id": candidate.asset_id,
            "provider": candidate.provider,
            "reason_codes": reasons,
            "overall_score": overall_score,
            "score_components": score_components,
            "selection_rationale": rationale,
            "commercial_use_verification_status": candidate.commercial_use_status,
            "licence_name": candidate.licence_name,
            "licence_evidence_url": candidate.licence_evidence_url,
            "original_asset_url": candidate.original_asset_url,
            "download_url": candidate.download_url,
            "watermarked": candidate.watermarked,
            "watermark_verification": (
                "detected" if candidate.watermarked else
                "clear" if candidate.watermarked is False else "unverified"
            ),
            "content_safety_status": candidate.content_safety_status,
            "content_safety_and_avoid_checks": checks,
            "provider_response": candidate.provider_response,
        }


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[\w']+", text.casefold(), flags=re.UNICODE)
        if token not in _STOPWORDS and len(token) > 1
    }


def _overlap(expected: set[str], actual: set[str]) -> float:
    if not expected:
        return 0.5
    return len(expected & actual) / len(expected)


def _is_https_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return (
        parsed.scheme.lower() == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and parsed.fragment == ""
    )
