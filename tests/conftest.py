from __future__ import annotations

import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scenepilot.config import Settings
from scenepilot.main import create_app
from scenepilot.services.url_safety import validate_public_https_url


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        log_level="CRITICAL",
        mock_provider=True,
        worker_enabled=True,
        worker_poll_seconds=0.02,
        max_upload_bytes=1024 * 1024,
        max_audio_seconds=600,
        max_scenes=10,
        max_request_bytes=2 * 1024 * 1024,
        input_directory=tmp_path / "inputs",
        database_url=f"sqlite:///{tmp_path / 'jobs.db'}",
        storage_health_url="",
        allowed_audio_hosts=frozenset(),
        url_fetch_timeout_seconds=1.0,
        url_fetch_max_redirects=2,
        idempotency_key_max_length=128,
        speech_recognition_provider="mock",
        tts_provider="mock",
        ffprobe_binary="ffprobe",
        ffmpeg_binary="ffmpeg",
        min_scene_seconds=1.5,
        max_scene_seconds=12.0,
        narration_boundary_tolerance_seconds=0.05,
        silence_threshold_dbfs=-55.0,
        mock_tts_words_per_minute=150,
        render_enabled=False,
        output_directory=tmp_path / "outputs",
        render_cache_directory=tmp_path / "render-cache",
        footage_directory=tmp_path / "footage",
    )


@pytest.fixture
def client(settings, monkeypatch):
    def public_dns(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(
        "scenepilot.services.url_safety.socket.getaddrinfo",
        public_dns,
    )
    monkeypatch.setattr(
        "scenepilot.api.routes.jobs.validate_public_https_url",
        lambda url, allow_hosts: url,
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client
