from __future__ import annotations

import pytest

from scenepilot.config import ConfigurationError, Settings


def test_environment_configuration_is_validated(monkeypatch, tmp_path):
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "4096")
    monkeypatch.setenv("MAX_AUDIO_SECONDS", "300")
    monkeypatch.setenv("INPUT_DIRECTORY", str(tmp_path / "uploads"))
    monkeypatch.setenv("OUTPUT_DIRECTORY", str(tmp_path / "outputs"))
    settings = Settings.from_env()
    assert settings.max_upload_bytes == 4096
    assert settings.max_audio_seconds == 300
    assert settings.input_directory == (tmp_path / "uploads").resolve()
    assert settings.render_enabled is False
    assert settings.output_directory == (tmp_path / "outputs").resolve()


def test_rendering_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("RENDER_ENABLED", "true")
    assert Settings.from_env().render_enabled is True


def test_footage_provider_rights_approval_requires_a_key(monkeypatch):
    monkeypatch.setenv("PEXELS_RIGHTS_POLICY_APPROVED", "true")
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    with pytest.raises(ConfigurationError, match="requires PEXELS_API_KEY"):
        Settings.from_env()


def test_footage_score_weights_must_sum_to_one(monkeypatch):
    monkeypatch.setenv("FOOTAGE_WEIGHT_SEMANTIC", "0.4")
    with pytest.raises(ConfigurationError, match="must sum to 1"):
        Settings.from_env()


def test_invalid_configuration_fails_explicitly(monkeypatch):
    monkeypatch.setenv("MAX_AUDIO_SECONDS", "601")
    with pytest.raises(ConfigurationError):
        Settings.from_env()
