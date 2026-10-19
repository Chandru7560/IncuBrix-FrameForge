"""Validated environment-driven service configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    """Raised when the runtime configuration is invalid."""


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized not in {"true", "false", "1", "0", "yes", "no"}:
        raise ConfigurationError(f"{name} must be a boolean")
    return normalized in {"true", "1", "yes"}


def _int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be numeric") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class Settings:
    app_env: str
    log_level: str
    mock_provider: bool
    worker_enabled: bool
    worker_poll_seconds: float
    max_upload_bytes: int
    max_audio_seconds: int
    max_scenes: int
    max_request_bytes: int
    input_directory: Path
    database_url: str
    storage_health_url: str
    allowed_audio_hosts: frozenset[str]
    url_fetch_timeout_seconds: float
    url_fetch_max_redirects: int
    idempotency_key_max_length: int
    speech_recognition_provider: str
    tts_provider: str
    ffprobe_binary: str
    ffmpeg_binary: str
    min_scene_seconds: float
    max_scene_seconds: float
    narration_boundary_tolerance_seconds: float
    silence_threshold_dbfs: float
    mock_tts_words_per_minute: int
    footage_source_order: tuple[str, ...] = ("pexels", "pixabay", "local")
    pexels_api_key: str = ""
    pixabay_api_key: str = ""
    pexels_rights_policy_approved: bool = False
    pixabay_rights_policy_approved: bool = False
    footage_min_relevance_score: float = 0.35
    footage_aspect_tolerance: float = 0.35
    footage_semantic_weight: float = 0.35
    footage_action_weight: float = 0.15
    footage_subject_weight: float = 0.15
    footage_composition_weight: float = 0.10
    footage_shot_variety_weight: float = 0.10
    footage_technical_weight: float = 0.15
    max_footage_asset_bytes: int = 100 * 1024 * 1024
    footage_download_timeout_seconds: float = 30.0
    footage_directory: Path | None = None
    max_footage_candidates_per_source: int = 10
    render_enabled: bool = True
    output_directory: Path | None = None
    render_cache_directory: Path | None = None
    render_timeout_seconds: float = 1800.0
    render_duration_tolerance_seconds: float = 0.08
    render_max_slowdown: float = 1.2
    relevance_evaluator_provider: str = "disabled"
    relevance_evaluator_base_url: str = ""
    relevance_evaluator_model: str = ""
    relevance_evaluator_api_key: str = ""
    relevance_evaluator_timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> Settings:
        database_url = os.getenv("DATABASE_URL", "sqlite:///./data/scenepilot.db")
        parsed_database_url = urlsplit(database_url)
        if parsed_database_url.scheme not in {"sqlite", "postgresql", "postgresql+psycopg2"}:
            raise ConfigurationError("DATABASE_URL must use SQLite or PostgreSQL")
        log_level = os.getenv("LOG_LEVEL", "INFO").upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("LOG_LEVEL is not a supported severity")

        configured_hosts = os.getenv("ALLOWED_AUDIO_HOSTS", "")
        allowed_hosts = frozenset(
            host.strip().lower().rstrip(".")
            for host in configured_hosts.split(",")
            if host.strip()
        )
        for host in allowed_hosts:
            if not host or any(character in host for character in "/:@?#"):
                raise ConfigurationError("ALLOWED_AUDIO_HOSTS contains an invalid host")

        mock_provider = _bool("MOCK_PROVIDER", True)
        if not mock_provider:
            raise ConfigurationError(
                "MOCK_PROVIDER=false is unsupported until a real provider adapter is configured"
            )

        speech_provider = os.getenv("SPEECH_RECOGNITION_PROVIDER", "mock").strip().lower()
        tts_provider = os.getenv("TTS_PROVIDER", "mock").strip().lower()
        if speech_provider != "mock":
            raise ConfigurationError(
                "SPEECH_RECOGNITION_PROVIDER must be 'mock' until a real adapter is configured"
            )
        if tts_provider != "mock":
            raise ConfigurationError("TTS_PROVIDER must be 'mock' until a real adapter is configured")

        source_order = tuple(
            item.strip().lower()
            for item in os.getenv("FOOTAGE_SOURCE_ORDER", "pexels,pixabay,local").split(",")
            if item.strip()
        )
        if not source_order or len(set(source_order)) != len(source_order):
            raise ConfigurationError("FOOTAGE_SOURCE_ORDER must contain unique source names")
        if any(source not in {"pexels", "pixabay", "local"} for source in source_order):
            raise ConfigurationError(
                "FOOTAGE_SOURCE_ORDER may contain only pexels, pixabay, and local"
            )
        weights = (
            _float("FOOTAGE_WEIGHT_SEMANTIC", 0.35, 0.0, 1.0),
            _float("FOOTAGE_WEIGHT_ACTION", 0.15, 0.0, 1.0),
            _float("FOOTAGE_WEIGHT_SUBJECT", 0.15, 0.0, 1.0),
            _float("FOOTAGE_WEIGHT_COMPOSITION", 0.10, 0.0, 1.0),
            _float("FOOTAGE_WEIGHT_SHOT_VARIETY", 0.10, 0.0, 1.0),
            _float("FOOTAGE_WEIGHT_TECHNICAL", 0.15, 0.0, 1.0),
        )
        if abs(sum(weights) - 1.0) > 1e-6:
            raise ConfigurationError("FOOTAGE_WEIGHT_* values must sum to 1")
        pexels_api_key = os.getenv("PEXELS_API_KEY", "").strip()
        pixabay_api_key = os.getenv("PIXABAY_API_KEY", "").strip()
        pexels_rights_policy_approved = _bool("PEXELS_RIGHTS_POLICY_APPROVED", False)
        pixabay_rights_policy_approved = _bool("PIXABAY_RIGHTS_POLICY_APPROVED", False)
        if pexels_rights_policy_approved and not pexels_api_key:
            raise ConfigurationError("PEXELS_RIGHTS_POLICY_APPROVED requires PEXELS_API_KEY")
        if pixabay_rights_policy_approved and not pixabay_api_key:
            raise ConfigurationError("PIXABAY_RIGHTS_POLICY_APPROVED requires PIXABAY_API_KEY")
        evaluator_provider = os.getenv("RELEVANCE_EVALUATOR_PROVIDER", "disabled").strip().lower()
        if evaluator_provider not in {"disabled", "openai_compatible"}:
            raise ConfigurationError(
                "RELEVANCE_EVALUATOR_PROVIDER must be disabled or openai_compatible"
            )
        evaluator_base_url = os.getenv("RELEVANCE_EVALUATOR_BASE_URL", "").strip().rstrip("/")
        evaluator_model = os.getenv("RELEVANCE_EVALUATOR_MODEL", "").strip()
        evaluator_api_key = os.getenv("RELEVANCE_EVALUATOR_API_KEY", "").strip()
        if evaluator_provider == "openai_compatible":
            parsed_evaluator_url = urlsplit(evaluator_base_url)
            loopback_http = parsed_evaluator_url.scheme == "http" and parsed_evaluator_url.hostname in {
                "127.0.0.1",
                "localhost",
                "::1",
            }
            if (
                parsed_evaluator_url.scheme != "https" and not loopback_http
            ) or not parsed_evaluator_url.netloc:
                raise ConfigurationError(
                    "RELEVANCE_EVALUATOR_BASE_URL must use HTTPS (HTTP is allowed for loopback)"
                )
            if not evaluator_model or not evaluator_api_key:
                raise ConfigurationError(
                    "Enabled relevance evaluation requires RELEVANCE_EVALUATOR_MODEL and RELEVANCE_EVALUATOR_API_KEY"
                )

        return cls(
            app_env=os.getenv("APP_ENV", "development"),
            log_level=log_level,
            mock_provider=mock_provider,
            worker_enabled=_bool("WORKER_ENABLED", True),
            worker_poll_seconds=_float("WORKER_POLL_SECONDS", 0.2, 0.02, 60.0),
            max_upload_bytes=_int("MAX_UPLOAD_BYTES", 25 * 1024 * 1024, 1, 2**31),
            max_audio_seconds=_int("MAX_AUDIO_SECONDS", 600, 1, 600),
            max_scenes=_int("MAX_SCENES", 100, 1, 1000),
            max_request_bytes=_int("MAX_REQUEST_BYTES", 26 * 1024 * 1024, 1024, 2**31),
            input_directory=Path(os.getenv("INPUT_DIRECTORY", "./data/inputs")).resolve(),
            database_url=database_url,
            storage_health_url=os.getenv("STORAGE_HEALTH_URL", "").strip(),
            allowed_audio_hosts=allowed_hosts,
            url_fetch_timeout_seconds=_float("URL_FETCH_TIMEOUT_SECONDS", 15.0, 0.1, 300.0),
            url_fetch_max_redirects=_int("URL_FETCH_MAX_REDIRECTS", 3, 0, 10),
            idempotency_key_max_length=_int("IDEMPOTENCY_KEY_MAX_LENGTH", 128, 1, 128),
            speech_recognition_provider=speech_provider,
            tts_provider=tts_provider,
            ffprobe_binary=os.getenv("FFPROBE_BINARY", "ffprobe"),
            ffmpeg_binary=os.getenv("FFMPEG_BINARY", "ffmpeg"),
            min_scene_seconds=_float("MIN_SCENE_SECONDS", 1.5, 0.5, 10.0),
            max_scene_seconds=_float("MAX_SCENE_SECONDS", 12.0, 2.0, 60.0),
            narration_boundary_tolerance_seconds=_float(
                "NARRATION_BOUNDARY_TOLERANCE_SECONDS", 0.05, 0.0, 0.5
            ),
            silence_threshold_dbfs=_float("SILENCE_THRESHOLD_DBFS", -55.0, -100.0, -10.0),
            mock_tts_words_per_minute=_int("MOCK_TTS_WORDS_PER_MINUTE", 150, 40, 400),
            footage_source_order=source_order,
            pexels_api_key=pexels_api_key,
            pixabay_api_key=pixabay_api_key,
            pexels_rights_policy_approved=pexels_rights_policy_approved,
            pixabay_rights_policy_approved=pixabay_rights_policy_approved,
            footage_min_relevance_score=_float(
                "FOOTAGE_MIN_RELEVANCE_SCORE", 0.35, 0.0, 1.0
            ),
            footage_aspect_tolerance=_float("FOOTAGE_ASPECT_TOLERANCE", 0.35, 0.0, 1.0),
            footage_semantic_weight=weights[0],
            footage_action_weight=weights[1],
            footage_subject_weight=weights[2],
            footage_composition_weight=weights[3],
            footage_shot_variety_weight=weights[4],
            footage_technical_weight=weights[5],
            max_footage_asset_bytes=_int(
                "MAX_FOOTAGE_ASSET_BYTES", 100 * 1024 * 1024, 1024, 2**31
            ),
            footage_download_timeout_seconds=_float(
                "FOOTAGE_DOWNLOAD_TIMEOUT_SECONDS", 30.0, 0.1, 300.0
            ),
            footage_directory=Path(
                os.getenv("FOOTAGE_DIRECTORY", str(Path(os.getenv("INPUT_DIRECTORY", "./data/inputs")) / "footage"))
            ).resolve(),
            max_footage_candidates_per_source=_int(
                "MAX_FOOTAGE_CANDIDATES_PER_SOURCE", 10, 1, 50
            ),
            render_enabled=_bool("RENDER_ENABLED", False),
            output_directory=Path(
                os.getenv("OUTPUT_DIRECTORY", "./data/outputs")
            ).resolve(),
            render_cache_directory=Path(
                os.getenv("RENDER_CACHE_DIRECTORY", "./data/render-cache")
            ).resolve(),
            render_timeout_seconds=_float("RENDER_TIMEOUT_SECONDS", 1800.0, 1.0, 86400.0),
            render_duration_tolerance_seconds=_float(
                "RENDER_DURATION_TOLERANCE_SECONDS", 0.08, 0.01, 1.0
            ),
            render_max_slowdown=_float("RENDER_MAX_SLOWDOWN", 1.2, 1.0, 2.0),
            relevance_evaluator_provider=evaluator_provider,
            relevance_evaluator_base_url=evaluator_base_url,
            relevance_evaluator_model=evaluator_model,
            relevance_evaluator_api_key=evaluator_api_key,
            relevance_evaluator_timeout_seconds=_float(
                "RELEVANCE_EVALUATOR_TIMEOUT_SECONDS", 30.0, 1.0, 300.0
            ),
        )


def ensure_input_directory(settings: Settings) -> None:
    try:
        settings.input_directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigurationError("INPUT_DIRECTORY is not writable") from exc
