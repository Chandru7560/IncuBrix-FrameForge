from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import httpx

from scenepilot.providers.footage import (
    DeterministicLocalFootageAdapter,
    FootageCandidate,
    FootageProviderError,
    FootageSearchResult,
    PexelsVideoAdapter,
    PixabayVideoAdapter,
)
from scenepilot.services.footage_download import (
    DownloadedFootage,
    FootageAssetDownloadError,
)
from scenepilot.services.visual_retrieval import (
    VisualRetrievalService,
    build_visual_search_description,
    policy_from_settings,
)


def make_candidate(
    asset_id: str,
    *,
    provider: str = "pexels",
    license_status: str = "verified",
    watermarked: bool | None = False,
    duration: float = 10,
    width: int = 1920,
    height: int = 1080,
    tags: tuple[str, ...] = ("cloud", "computing", "data", "server"),
    safety_status: str = "metadata_screened",
    shot_type: str = "medium shot",
) -> FootageCandidate:
    verified = license_status == "verified"
    return FootageCandidate(
        asset_id=asset_id,
        provider=provider,
        title="Cloud computing data server",
        description="Data moves between cloud servers",
        tags=tags,
        original_asset_url=f"https://{provider}.example/videos/{asset_id}",
        download_url=f"https://{provider}.example/cdn/{asset_id}.mp4",
        duration_seconds=duration,
        width=width,
        height=height,
        licence_name="Provider content licence" if verified else None,
        licence_evidence_url="https://provider.example/license" if verified else None,
        attribution_required=True,
        attribution_text=f"Video {asset_id} from {provider}",
        commercial_use_status=license_status,
        commercial_use_verified_at=(
            datetime.now(timezone.utc).isoformat() if verified else None
        ),
        retrieved_at=datetime.now(timezone.utc).isoformat(),
        source_terms_reference="https://provider.example/terms",
        ai_generated=False,
        watermarked=watermarked,
        content_safety_status=safety_status,
        provider_response={"id": asset_id, "metadata": "fixture"},
        shot_type=shot_type,
    )


class StaticSource:
    def __init__(self, name: str, candidates=(), error=None):
        self.name = name
        self.candidates = tuple(candidates)
        self.error = error
        self.queries = []

    def search(self, query: str, *, limit: int) -> FootageSearchResult:
        self.queries.append(query)
        if self.error:
            raise self.error
        return FootageSearchResult(
            provider=self.name,
            candidates=self.candidates[:limit],
            response_metadata={"request_id": f"{self.name}-test"},
        )


def downloader(path: Path):
    def download(candidate):
        return DownloadedFootage(
            path=path,
            size_bytes=1234,
            sha256="c" * 64,
            width=candidate.width,
            height=candidate.height,
        )

    return download


def make_scene():
    return {
        "scene_id": "scene_001",
        "narration_text": "Cloud computing moves data between remote servers.",
        "duration_seconds": 4.0,
        "intent_graph": {
            "central_concept": "cloud computing",
            "interpretation": "Remote computing services",
            "intent": "Show data moving between devices and remote servers.",
            "entities": ["data", "servers"],
            "actions": ["moves"],
            "desired_shot_type": "medium shot",
            "visual_hint": "Server room",
            "visual_hint_assessment": {"alignment": "reinforces"},
        },
    }


def service_settings(settings, **changes):
    return replace(
        settings,
        footage_source_order=("pexels", "pixabay", "local"),
        footage_min_relevance_score=0.0,
        footage_aspect_tolerance=0.5,
        **changes,
    )


def test_visual_query_uses_meaning_context_and_nonconflicting_hint():
    query = build_visual_search_description(
        make_scene()["intent_graph"],
        make_scene()["narration_text"],
        visual_style="natural documentary",
    )
    assert "remote servers" in query
    assert "cloud computing" in query
    assert "Server room" in query
    assert "natural documentary" in query


def test_local_adapter_is_deterministic_and_never_publishable():
    adapter = DeterministicLocalFootageAdapter()
    first = adapter.search("trust in business", limit=10)
    second = adapter.search("trust in business", limit=10)
    assert replace(
        first,
        candidates=tuple(replace(candidate, retrieved_at="") for candidate in first.candidates),
    ) == replace(
        second,
        candidates=tuple(replace(candidate, retrieved_at="") for candidate in second.candidates),
    )
    assert first.candidates
    assert all(candidate.test_fixture for candidate in first.candidates)
    assert all(candidate.commercial_use_status == "unverified" for candidate in first.candidates)


def test_offline_fixtures_are_recorded_as_rejected_not_real_footage(settings):
    adapter = DeterministicLocalFootageAdapter()
    result = VisualRetrievalService(
        service_settings(settings),
        sources=(adapter,),
    ).select(
        make_scene(),
        aspect_ratio="16:9",
        avoid=[],
        selected_asset_ids=set(),
    )
    assert result["status"] == "no_eligible_asset"
    assert result["rejected_alternatives"]
    assert "test_fixture_not_publishable" in result["rejected_alternatives"][0]["reason_codes"]
    assert result["rights_passport"] is None


def test_provider_outage_falls_through_to_next_provider(settings, tmp_path):
    down = StaticSource(
        "pexels",
        error=FootageProviderError("pexels", "outage", retryable=True),
    )
    working = StaticSource("pixabay", [make_candidate("good-1", provider="pixabay")])
    result = VisualRetrievalService(
        service_settings(settings),
        sources=(down, working),
        downloader=downloader(tmp_path / "valid.mp4"),
    ).select(
        make_scene(),
        aspect_ratio="16:9",
        avoid=[],
        selected_asset_ids=set(),
    )
    assert result["status"] == "selected"
    assert [attempt["outcome"] for attempt in result["source_attempts"]] == [
        "unavailable",
        "selected",
    ]
    assert result["rights_passport"]["provider"] == "pixabay"


def test_empty_search_falls_through_to_next_provider(settings, tmp_path):
    empty = StaticSource("pexels")
    working = StaticSource("pixabay", [make_candidate("good-2", provider="pixabay")])
    result = VisualRetrievalService(
        service_settings(settings),
        sources=(empty, working),
        downloader=downloader(tmp_path / "valid.mp4"),
    ).select(
        make_scene(),
        aspect_ratio="16:9",
        avoid=[],
        selected_asset_ids=set(),
    )
    assert [attempt["outcome"] for attempt in result["source_attempts"]] == [
        "empty_results",
        "selected",
    ]


def test_unverified_licence_is_rejected_and_verified_source_is_tried_next(
    settings, tmp_path
):
    unknown = StaticSource(
        "pexels",
        [make_candidate("unknown", license_status="unverified")],
    )
    licensed = StaticSource("pixabay", [make_candidate("licensed", provider="pixabay")])
    result = VisualRetrievalService(
        service_settings(settings),
        sources=(unknown, licensed),
        downloader=downloader(tmp_path / "valid.mp4"),
    ).select(
        make_scene(),
        aspect_ratio="16:9",
        avoid=[],
        selected_asset_ids=set(),
    )
    assert result["status"] == "selected"
    rejected = result["rejected_alternatives"][0]
    assert rejected["commercial_use_verification_status"] == "unverified"
    assert "commercial_rights_unverified_or_incomplete" in rejected["reason_codes"]


def test_missing_or_unknown_watermark_status_is_never_selected(settings, tmp_path):
    source = StaticSource("pexels", [make_candidate("watermark-unknown", watermarked=None)])
    result = VisualRetrievalService(
        service_settings(settings),
        sources=(source,),
        downloader=downloader(tmp_path / "valid.mp4"),
    ).select(
        make_scene(),
        aspect_ratio="16:9",
        avoid=[],
        selected_asset_ids=set(),
    )
    assert result["status"] == "no_eligible_asset"
    assert "watermark_status_unverified" in result["rejected_alternatives"][0]["reason_codes"]


def test_watermarked_asset_is_rejected(settings, tmp_path):
    source = StaticSource("pexels", [make_candidate("watermarked", watermarked=True)])
    result = VisualRetrievalService(
        service_settings(settings),
        sources=(source,),
        downloader=downloader(tmp_path / "valid.mp4"),
    ).select(
        make_scene(),
        aspect_ratio="16:9",
        avoid=[],
        selected_asset_ids=set(),
    )
    assert "watermarked_asset" in result["rejected_alternatives"][0]["reason_codes"]


def test_corrupt_download_falls_back_to_next_ranked_candidate(settings, tmp_path):
    source = StaticSource("pexels", [make_candidate("corrupt"), make_candidate("valid")])

    def download(candidate):
        if candidate.asset_id == "corrupt":
            raise FootageAssetDownloadError(
                "CORRUPTED_FOOTAGE_DOWNLOAD", "fixture decoder rejected bytes"
            )
        return DownloadedFootage(tmp_path / "good.mp4", 10, "d" * 64, 1920, 1080)

    result = VisualRetrievalService(
        service_settings(settings),
        sources=(source,),
        downloader=download,
    ).select(
        make_scene(),
        aspect_ratio="16:9",
        avoid=[],
        selected_asset_ids=set(),
    )
    assert result["status"] == "selected"
    assert result["rights_passport"]["asset_id"] == "valid"
    assert any(
        "download:CORRUPTED_FOOTAGE_DOWNLOAD" in rejection["reason_codes"]
        for rejection in result["rejected_alternatives"]
    )


def test_duplicate_asset_is_rejected_and_next_candidate_selected(settings, tmp_path):
    source = StaticSource("pexels", [make_candidate("already-used"), make_candidate("new")])
    result = VisualRetrievalService(
        service_settings(settings),
        sources=(source,),
        downloader=downloader(tmp_path / "good.mp4"),
    ).select(
        make_scene(),
        aspect_ratio="16:9",
        avoid=[],
        selected_asset_ids={"pexels:already-used"},
    )
    assert result["status"] == "selected"
    assert result["rights_passport"]["asset_id"] == "new"
    assert "duplicate_asset_already_selected_for_this_output" in result["rejected_alternatives"][0]["reason_codes"]


def test_aspect_duration_avoid_and_safety_rules_filter_candidates(settings, tmp_path):
    source = StaticSource(
        "pexels",
        [
            make_candidate("portrait", width=1080, height=1920),
            make_candidate("short", duration=1),
            make_candidate("avoid", tags=("cloud", "computing", "logo")),
            make_candidate("unsafe", safety_status="blocked"),
            make_candidate("valid"),
        ],
    )
    result = VisualRetrievalService(
        service_settings(settings),
        sources=(source,),
        downloader=downloader(tmp_path / "good.mp4"),
    ).select(
        make_scene(),
        aspect_ratio="16:9",
        avoid=["logo"],
        selected_asset_ids=set(),
    )
    assert result["status"] == "selected"
    reasons = {
        reason
        for item in result["rejected_alternatives"]
        for reason in item["reason_codes"]
    }
    assert "aspect_ratio_outside_policy_tolerance" in reasons
    assert "asset_shorter_than_scene" in reasons
    assert "avoid_rule_matched:logo" in reasons
    assert "content_safety_blocked" in reasons


def test_provider_response_cannot_verify_rights_without_operator_policy_approval(monkeypatch):
    adapter = PexelsVideoAdapter("test-secret", rights_policy_approved=False)
    payload = {
        "videos": [
            {
                "id": 12,
                "url": "https://www.pexels.com/video/cloud-computing-12/",
                "duration": 10,
                "user": {"name": "Test Artist"},
                "video_files": [
                    {
                        "file_type": "video/mp4",
                        "link": "https://videos.pexels.com/files/12.mp4",
                        "width": 1920,
                        "height": 1080,
                    }
                ],
            }
        ]
    }
    monkeypatch.setattr(
        adapter,
        "_request",
        lambda query, limit: httpx.Response(
            200, json=payload, request=httpx.Request("GET", adapter.endpoint)
        ),
    )
    result = adapter.search("cloud computing", limit=5)
    candidate = result.candidates[0]
    assert candidate.commercial_use_status == "unverified"
    assert candidate.licence_name is None
    assert candidate.commercial_use_verified_at is None
    assert "test-secret" not in str(candidate.provider_response)


def test_approved_provider_policy_records_licence_and_verification_timestamp(monkeypatch):
    adapter = PixabayVideoAdapter("test-secret", rights_policy_approved=True)
    payload = {
        "hits": [
            {
                "id": 20,
                "pageURL": "https://pixabay.com/videos/cloud-data-20/",
                "duration": 8,
                "user": "Test Artist",
                "tags": "cloud, computing, data",
                "videos": {
                    "medium": {
                        "url": "https://cdn.pixabay.com/video/20.mp4",
                        "width": 1280,
                        "height": 720,
                        "size": 1000,
                    }
                },
            }
        ]
    }
    monkeypatch.setattr(
        adapter,
        "_request",
        lambda query, limit: httpx.Response(
            200, json=payload, request=httpx.Request("GET", adapter.endpoint)
        ),
    )
    candidate = adapter.search("cloud", limit=5).candidates[0]
    assert candidate.commercial_use_status == "verified"
    assert candidate.licence_name == "Pixabay Content License"
    assert candidate.licence_evidence_url == "https://pixabay.com/service/terms/"
    assert candidate.commercial_use_verified_at
    assert candidate.watermarked is None


def test_configured_score_policy_sums_to_one(settings):
    weights = policy_from_settings(settings).weights()
    assert sum(weights.values()) == 1.0
