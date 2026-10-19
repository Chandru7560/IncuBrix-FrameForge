"""Stock-footage search adapters and rights-aware candidate metadata."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import unquote, urlsplit

import httpx

logger = logging.getLogger("scenepilot.footage")

PEXELS_LICENSE_URL = "https://www.pexels.com/license/"
PIXABAY_LICENSE_URL = "https://pixabay.com/service/terms/"


def utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class FootageCandidate:
    asset_id: str
    provider: str
    title: str
    description: str
    tags: tuple[str, ...]
    original_asset_url: str | None
    download_url: str | None
    duration_seconds: float
    width: int
    height: int
    licence_name: str | None
    licence_evidence_url: str | None
    attribution_required: bool | None
    attribution_text: str | None
    commercial_use_status: str
    commercial_use_verified_at: str | None
    retrieved_at: str
    source_terms_reference: str | None
    ai_generated: bool
    watermarked: bool | None
    content_safety_status: str
    provider_response: dict[str, object]
    test_fixture: bool = False
    shot_type: str | None = None


@dataclass(frozen=True)
class FootageSearchResult:
    provider: str
    candidates: tuple[FootageCandidate, ...]
    response_metadata: dict[str, object]


class FootageProviderError(RuntimeError):
    def __init__(self, provider: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable


class FootageSourceAdapter(Protocol):
    name: str

    def search(self, query: str, *, limit: int) -> FootageSearchResult: ...


class _StockVideoAdapter:
    name: str
    endpoint: str
    license_name: str
    license_url: str
    api_timeout_seconds = 15.0
    _blocked_terms = {
        "deepfake",
        "explicit",
        "gore",
        "graphic violence",
        "nonconsensual",
        "weapon",
    }

    def __init__(self, api_key: str, *, rights_policy_approved: bool) -> None:
        if not api_key.strip():
            raise ValueError(f"{self.name} API key is required")
        self._api_key = api_key
        self._rights_policy_approved = rights_policy_approved

    def search(self, query: str, *, limit: int) -> FootageSearchResult:
        try:
            response = self._request(query, limit)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            logger.warning(
                "footage_provider_search_failed",
                extra={"provider": self.name, "upstream_status": status},
            )
            raise FootageProviderError(
                self.name,
                "Footage provider search failed.",
                retryable=status is None or status >= 500 or status == 429,
            ) from exc
        if not isinstance(payload, dict):
            raise FootageProviderError(
                self.name, "Footage provider returned an invalid response.", retryable=False
            )
        candidates = tuple(
            candidate
            for item in self._items(payload)
            if (candidate := self._candidate(item)) is not None
        )
        return FootageSearchResult(
            provider=self.name,
            candidates=candidates[:limit],
            response_metadata={
                "status_code": response.status_code,
                "candidate_count": len(candidates),
                "provider": self.name,
            },
        )

    def _request(self, query: str, limit: int) -> httpx.Response:
        raise NotImplementedError

    def _items(self, payload: dict) -> list[dict]:
        raise NotImplementedError

    def _candidate(self, item: dict) -> FootageCandidate | None:
        raise NotImplementedError

    def _make_candidate(
        self,
        *,
        asset_id: object,
        page_url: object,
        download_url: object,
        title: str,
        description: str,
        tags: tuple[str, ...],
        duration: object,
        width: object,
        height: object,
        attribution: str | None,
        attribution_required: bool,
        provider_response: dict[str, object],
        shot_type: str | None = None,
    ) -> FootageCandidate | None:
        asset_id_text = str(asset_id).strip()
        page = page_url if _is_https_url(page_url) else None
        download = download_url if _is_https_url(download_url) else None
        try:
            seconds = float(duration)
            pixel_width = int(width)
            pixel_height = int(height)
        except (TypeError, ValueError):
            return None
        if not asset_id_text or seconds <= 0 or pixel_width <= 0 or pixel_height <= 0:
            return None
        metadata = " ".join((title, description, *tags)).casefold()
        blocked = any(term in metadata for term in self._blocked_terms)
        policy_verified = (
            self._rights_policy_approved
            and bool(page)
            and bool(download)
            and bool(self.license_name)
            and bool(self.license_url)
        )
        return FootageCandidate(
            asset_id=asset_id_text,
            provider=self.name,
            title=title,
            description=description,
            tags=tags,
            original_asset_url=page,
            download_url=download,
            duration_seconds=seconds,
            width=pixel_width,
            height=pixel_height,
            licence_name=self.license_name if policy_verified else None,
            licence_evidence_url=self.license_url if policy_verified else None,
            attribution_required=attribution_required,
            attribution_text=attribution,
            commercial_use_status="verified" if policy_verified else "unverified",
            commercial_use_verified_at=utc_iso_now() if policy_verified else None,
            retrieved_at=utc_iso_now(),
            source_terms_reference=self.license_url,
            ai_generated=False,
            watermarked=None,
            content_safety_status="blocked" if blocked else "metadata_screened",
            provider_response=provider_response,
            shot_type=shot_type,
        )


class PexelsVideoAdapter(_StockVideoAdapter):
    name = "pexels"
    endpoint = "https://api.pexels.com/videos/search"
    license_name = "Pexels License"
    license_url = PEXELS_LICENSE_URL

    def _request(self, query: str, limit: int) -> httpx.Response:
        return httpx.get(
            self.endpoint,
            params={"query": query, "per_page": limit},
            headers={"Authorization": self._api_key},
            timeout=self.api_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )

    def _items(self, payload: dict) -> list[dict]:
        return [item for item in payload.get("videos", []) if isinstance(item, dict)]

    def _candidate(self, item: dict) -> FootageCandidate | None:
        files = item.get("video_files")
        if not isinstance(files, list):
            return None
        files = [
            entry
            for entry in files
            if isinstance(entry, dict)
            and str(entry.get("file_type", "")).lower() in {"video/mp4", "video/webm"}
            and _is_https_url(entry.get("link"))
        ]
        if not files:
            return None
        video_file = max(
            files,
            key=lambda entry: int(entry.get("width") or 0) * int(entry.get("height") or 0),
        )
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        photographer = str(user.get("name") or "").strip() or None
        response = {
            "id": item.get("id"),
            "url": item.get("url"),
            "duration": item.get("duration"),
            "width": video_file.get("width"),
            "height": video_file.get("height"),
            "file_type": video_file.get("file_type"),
            "photographer": photographer,
        }
        return self._make_candidate(
            asset_id=item.get("id"),
            page_url=item.get("url"),
            download_url=video_file.get("link"),
            title=unquote(urlsplit(str(item.get("url") or "")).path.rsplit("/", 1)[-1])
            .replace("-", " ")
            .strip()
            or "Pexels video",
            description="",
            tags=(),
            duration=item.get("duration"),
            width=video_file.get("width"),
            height=video_file.get("height"),
            attribution=(
                f"Video by {photographer} on Pexels"
                if photographer
                else "Video from Pexels"
            ),
            attribution_required=False,
            provider_response=response,
            shot_type=None,
        )


class PixabayVideoAdapter(_StockVideoAdapter):
    name = "pixabay"
    endpoint = "https://pixabay.com/api/videos/"
    license_name = "Pixabay Content License"
    license_url = PIXABAY_LICENSE_URL

    def _request(self, query: str, limit: int) -> httpx.Response:
        return httpx.get(
            self.endpoint,
            params={"key": self._api_key, "q": query, "per_page": limit},
            timeout=self.api_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )

    def _items(self, payload: dict) -> list[dict]:
        return [item for item in payload.get("hits", []) if isinstance(item, dict)]

    def _candidate(self, item: dict) -> FootageCandidate | None:
        videos = item.get("videos")
        if not isinstance(videos, dict):
            return None
        variants = [entry for entry in videos.values() if isinstance(entry, dict)]
        variants = [
            entry
            for entry in variants
            if _is_https_url(entry.get("url"))
            and entry.get("width")
            and entry.get("height")
        ]
        if not variants:
            return None
        video_file = max(
            variants,
            key=lambda entry: int(entry.get("width") or 0) * int(entry.get("height") or 0),
        )
        tags = tuple(
            tag.strip()
            for tag in str(item.get("tags") or "").split(",")
            if tag.strip()
        )
        creator = str(item.get("user") or "").strip() or None
        response = {
            "id": item.get("id"),
            "page_url": item.get("pageURL"),
            "duration": item.get("duration"),
            "width": video_file.get("width"),
            "height": video_file.get("height"),
            "tags": list(tags),
            "creator": creator,
        }
        return self._make_candidate(
            asset_id=item.get("id"),
            page_url=item.get("pageURL"),
            download_url=video_file.get("url"),
            title=" ".join(tags) or "Pixabay video",
            description="",
            tags=tags,
            duration=item.get("duration"),
            width=video_file.get("width"),
            height=video_file.get("height"),
            attribution=(
                f"Video by {creator} on Pixabay" if creator else "Video from Pixabay"
            ),
            attribution_required=True,
            provider_response=response,
        )


class DeterministicLocalFootageAdapter:
    """Return synthetic metadata fixtures only; never a real or publishable asset."""

    name = "local"

    def search(self, query: str, *, limit: int) -> FootageSearchResult:
        words = tuple(word for word in query.casefold().split() if len(word) > 3)
        fixtures = tuple(
            FootageCandidate(
                asset_id=f"local-fixture-{index}",
                provider=self.name,
                title=f"Offline metadata fixture {index}",
                description="Synthetic test metadata; no footage file is present.",
                tags=words[:4],
                original_asset_url=None,
                download_url=None,
                duration_seconds=30.0,
                width=1920,
                height=1080,
                licence_name=None,
                licence_evidence_url=None,
                attribution_required=None,
                attribution_text=None,
                commercial_use_status="unverified",
                commercial_use_verified_at=None,
                retrieved_at=utc_iso_now(),
                source_terms_reference=None,
                ai_generated=False,
                watermarked=False,
                content_safety_status="unknown",
                provider_response={"fixture": True, "query_terms": list(words[:4])},
                test_fixture=True,
            )
            for index in range(1, min(limit, 2) + 1)
        )
        return FootageSearchResult(
            provider=self.name,
            candidates=fixtures,
            response_metadata={"fixture": True, "candidate_count": len(fixtures)},
        )


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
