from __future__ import annotations

import socket
import subprocess

import pytest

from scenepilot.providers.footage import FootageCandidate
from scenepilot.services.footage_download import (
    FootageAssetDownloadError,
    download_and_validate_footage,
)


class FakeResponse:
    status = 200

    def __init__(self):
        self.chunks = [b"corrupt-video!!", b""]

    def getheader(self, name, default=None):
        return {
            "Content-Type": "video/mp4",
            "Content-Length": "15",
        }.get(name, default)

    def read(self, size):
        return self.chunks.pop(0)


class FakeConnection:
    def __init__(self, host, pinned_ip, *, timeout):
        self.response = FakeResponse()

    def request(self, method, path, headers=None):
        pass

    def getresponse(self):
        return self.response

    def close(self):
        pass


def test_corrupted_download_is_deleted_and_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "scenepilot.services.footage_download.socket.getaddrinfo",
        lambda host, port, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
        ],
    )
    monkeypatch.setattr(
        "scenepilot.services.footage_download._PinnedHTTPSConnection",
        FakeConnection,
    )
    monkeypatch.setattr(
        "scenepilot.services.footage_download.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, stdout=b"", stderr=b"invalid MP4"
        ),
    )
    candidate = FootageCandidate(
        asset_id="broken",
        provider="pexels",
        title="fixture",
        description="",
        tags=(),
        original_asset_url="https://www.pexels.com/video/broken/",
        download_url="https://videos.pexels.com/files/broken.mp4",
        duration_seconds=4,
        width=1920,
        height=1080,
        licence_name="Pexels License",
        licence_evidence_url="https://www.pexels.com/license/",
        attribution_required=False,
        attribution_text="test",
        commercial_use_status="verified",
        commercial_use_verified_at="2026-01-01T00:00:00+00:00",
        retrieved_at="2026-01-01T00:00:00+00:00",
        source_terms_reference="https://www.pexels.com/license/",
        ai_generated=False,
        watermarked=False,
        content_safety_status="metadata_screened",
        provider_response={"id": "broken"},
    )
    with pytest.raises(FootageAssetDownloadError) as error:
        download_and_validate_footage(
            candidate,
            destination_directory=tmp_path / "assets",
            max_bytes=100,
            timeout_seconds=5,
            ffprobe_binary="ffprobe",
        )
    assert error.value.code == "CORRUPTED_FOOTAGE_DOWNLOAD"
    assert list((tmp_path / "assets").iterdir()) == []


def test_download_rejects_non_https_url_before_dns_lookup(monkeypatch, tmp_path):
    candidate = FootageCandidate(
        asset_id="insecure",
        provider="pexels",
        title="fixture",
        description="",
        tags=(),
        original_asset_url="https://www.pexels.com/video/insecure/",
        download_url="http://videos.pexels.com/files/insecure.mp4",
        duration_seconds=4,
        width=1920,
        height=1080,
        licence_name=None,
        licence_evidence_url=None,
        attribution_required=False,
        attribution_text=None,
        commercial_use_status="unverified",
        commercial_use_verified_at=None,
        retrieved_at="2026-01-01T00:00:00+00:00",
        source_terms_reference=None,
        ai_generated=False,
        watermarked=None,
        content_safety_status="unknown",
        provider_response={},
    )
    monkeypatch.setattr(
        "scenepilot.services.footage_download.socket.getaddrinfo",
        lambda *args, **kwargs: pytest.fail("DNS must not be used for an invalid URL"),
    )
    with pytest.raises(FootageAssetDownloadError) as error:
        download_and_validate_footage(
            candidate,
            destination_directory=tmp_path / "assets",
            max_bytes=100,
            timeout_seconds=5,
            ffprobe_binary="ffprobe",
        )
    assert error.value.code == "DOWNLOAD_URL_REJECTED"
    assert not (tmp_path / "assets").exists()
