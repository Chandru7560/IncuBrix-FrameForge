"""Job creation and status routes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException

from scenepilot.api.schemas import (
    ErrorResponse,
    JobAccepted,
    JobCreateRequest,
    JobOptions,
    JobStatus,
    SceneRatingRequest,
    SceneRatingResponse,
)
from scenepilot.config import Settings
from scenepilot.errors import ApiError
from scenepilot.models import IdempotencyRecord, Job, SceneRating
from scenepilot.observability import correlation_id_context
from scenepilot.services.input_validation import validate_audio_upload
from scenepilot.services.url_safety import validate_public_https_url

router = APIRouter(prefix="/api/v1", tags=["jobs"])
logger = logging.getLogger("scenepilot.api.jobs")

def _validate_idempotency_key(value: str, settings: Settings) -> str:
    if (
        not 1 <= len(value) <= settings.idempotency_key_max_length
        or re.fullmatch(r"[\x21-\x7e]+", value) is None
    ):
        raise ApiError(
            400,
            "INVALID_IDEMPOTENCY_KEY",
            "Idempotency-Key must contain 1 to the configured maximum printable ASCII characters.",
            details={"max_length": settings.idempotency_key_max_length},
        )
    return value


def _normalize_request(request: JobCreateRequest) -> dict:
    data = request.model_dump(mode="json", exclude_none=True)
    if data.get("audio_url"):
        data["audio_url"] = validate_public_https_url(data["audio_url"], allow_hosts=frozenset())
    return data


def _fingerprint(request_data: dict, upload_sha256: str | None = None) -> str:
    canonical = json.dumps(
        {"request": request_data, "upload_sha256": upload_sha256},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _existing_job(session, key: str, request_hash: str) -> Job | None:
    record = session.scalar(
        select(IdempotencyRecord).where(IdempotencyRecord.key == key)
    )
    if record is None:
        return None
    if record.request_hash != request_hash:
        raise ApiError(
            409,
            "IDEMPOTENCY_KEY_REUSED",
            "This Idempotency-Key was already used with a different request.",
        )
    return session.get(Job, record.job_id)


def _create_idempotent_job(
    request: Request,
    *,
    key: str,
    request_hash: str,
    input_type: str,
    request_data: dict,
) -> Job:
    session_factory = request.app.state.session_factory
    correlation_id = correlation_id_context.get()
    try:
        with session_factory.begin() as session:
            existing = _existing_job(session, key, request_hash)
            if existing is not None:
                return existing
            job = Job(
                status="queued",
                progress=0,
                input_type=input_type,
                request_data=request_data,
                correlation_id=correlation_id,
            )
            session.add(job)
            session.flush()
            session.add(
                IdempotencyRecord(
                    key=key,
                    request_hash=request_hash,
                    job_id=job.id,
                )
            )
            session.flush()
            session.expunge(job)
            created = job
        logger.info(
            "job_queued",
            extra={"job_id": created.id, "correlation_id": correlation_id},
        )
        return created
    except IntegrityError:
        with session_factory() as session:
            existing = _existing_job(session, key, request_hash)
            if existing is not None:
                return existing
        raise ApiError(
            503,
            "IDEMPOTENCY_CONFLICT",
            "The job could not be safely deduplicated; retry the request.",
            retryable=True,
        )


def _accepted(job: Job) -> JSONResponse:
    return JSONResponse(
        status_code=202,
        content={
            "job_id": job.id,
            "status": job.status,
            "progress": job.progress,
            "status_url": f"/api/v1/jobs/{job.id}",
        },
    )


def _parse_json_payload(raw: object, settings: Settings) -> JobCreateRequest:
    if not isinstance(raw, dict):
        raise ApiError(400, "INVALID_JSON", "The JSON request body must be an object.")
    if isinstance(raw.get("scenes"), list) and len(raw["scenes"]) > settings.max_scenes:
        raise ApiError(
            422,
            "TOO_MANY_SCENES",
            "The scene list exceeds the configured maximum.",
            details={"max_scenes": settings.max_scenes},
        )
    try:
        return JobCreateRequest.model_validate(raw)
    except ValidationError as exc:
        details = [
            {
                "location": list(error.get("loc", ())),
                "message": error.get("msg", "Invalid value"),
                "type": error.get("type", "value_error"),
            }
            for error in exc.errors()
        ]
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            "The request did not satisfy the job input schema.",
            details={"errors": details},
        ) from exc


def _parse_multipart_options(raw: dict) -> JobOptions:
    try:
        return JobOptions.model_validate(raw)
    except ValidationError as exc:
        details = [
            {
                "location": list(error.get("loc", ())),
                "message": error.get("msg", "Invalid value"),
                "type": error.get("type", "value_error"),
            }
            for error in exc.errors()
        ]
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            "The request did not satisfy the job options schema.",
            details={"errors": details},
        ) from exc


async def _validate_remote_url(url: str, settings: Settings) -> str:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                validate_public_https_url,
                url,
                allow_hosts=settings.allowed_audio_hosts,
            ),
            timeout=min(1.5, settings.url_fetch_timeout_seconds),
        )
    except asyncio.TimeoutError as exc:
        raise ApiError(
            503,
            "URL_VALIDATION_TIMEOUT",
            "The URL host could not be checked within the validation time limit.",
            retryable=True,
        ) from exc


def _parse_optional_json(value: str | None, field_name: str):
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            f"{field_name} must be sent as a text field.",
            details={"field": field_name},
        )
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            f"{field_name} must contain valid JSON.",
            details={"field": field_name},
        ) from exc


@router.post(
    "/broll/jobs",
    status_code=202,
    response_model=JobAccepted,
    operation_id="create_broll_job",
    summary="Create an asynchronous B-roll job",
    openapi_extra={
        "parameters": [
            {
                "name": "Idempotency-Key",
                "in": "header",
                "required": True,
                "description": "Required token used to deduplicate identical job submissions.",
                "schema": {"type": "string", "minLength": 1, "maxLength": 128},
            }
        ],
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "oneOf": [
                            {
                                "type": "object",
                                "required": ["script"],
                                "properties": {
                                    "script": {"type": "string", "minLength": 1, "maxLength": 100000}
                                },
                            },
                            {
                                "type": "object",
                                "required": ["scenes"],
                                "properties": {
                                    "scenes": {
                                        "type": "array",
                                        "minItems": 1,
                                        "items": {
                                            "type": "object",
                                            "required": ["text"],
                                            "properties": {
                                                "text": {"type": "string", "minLength": 1, "maxLength": 5000},
                                                "visual_hint": {"type": ["string", "null"], "maxLength": 1000},
                                            },
                                            "additionalProperties": False,
                                        },
                                    }
                                },
                            },
                            {
                                "type": "object",
                                "required": ["audio_url"],
                                "properties": {"audio_url": {"type": "string", "format": "uri"}},
                            },
                        ],
                        "properties": {
                            "voice": {"type": "string", "default": "en-IN-female"},
                            "aspect_ratios": {
                                "type": "array",
                                "items": {"type": "string", "enum": ["16:9", "9:16", "1:1"]},
                            },
                            "style": {"type": "string"},
                            "avoid": {"type": "array", "items": {"type": "string"}},
                            "captions": {
                                "type": "object",
                                "properties": {
                                    "burn_in": {"type": "boolean", "default": True},
                                    "srt": {"type": "boolean", "default": True},
                                },
                            },
                        },
                        "additionalProperties": False,
                    }
                },
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {
                            "file": {"type": "string", "format": "binary"},
                            "voice": {"type": "string"},
                            "aspect_ratios": {"type": "string", "description": "JSON array of output ratios."},
                            "style": {"type": "string"},
                            "avoid": {"type": "string", "description": "JSON array of avoid phrases."},
                            "captions": {"type": "string", "description": "JSON object of caption settings."},
                            "scenes": {"type": "string", "description": "Reserved; not accepted with file."},
                            "script": {"type": "string", "description": "Reserved; not accepted with file."},
                            "audio_url": {"type": "string", "description": "Reserved; not accepted with file."},
                        },
                        "additionalProperties": False,
                    }
                },
            },
        }
    },
    responses={
        202: {"description": "Job accepted and queued."},
        400: {"model": ErrorResponse, "description": "Invalid idempotency key or JSON body."},
        409: {
            "model": ErrorResponse,
            "description": "Idempotency key was reused for a different request.",
        },
        413: {
            "model": ErrorResponse,
            "description": "Request or uploaded file exceeds configured limits.",
        },
        415: {"model": ErrorResponse, "description": "Uploaded audio format is not supported."},
        422: {"model": ErrorResponse, "description": "Input is invalid or unsafe."},
        503: {"model": ErrorResponse, "description": "The job could not be safely queued."},
    },
)
async def create_broll_job(
    request: Request,
) -> JSONResponse:
    settings: Settings = request.app.state.settings
    supplied_key = request.headers.get("Idempotency-Key")
    if supplied_key is None:
        raise ApiError(400, "MISSING_IDEMPOTENCY_KEY", "The Idempotency-Key header is required.")
    key = _validate_idempotency_key(supplied_key, settings)
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()

    if content_type == "application/json":
        try:
            raw = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError(400, "INVALID_JSON", "The request body is not valid JSON.") from exc
        job_request = _parse_json_payload(raw, settings)
        request_data = job_request.model_dump(mode="json", exclude_none=True)
        input_type = "audio_url" if job_request.audio_url else "script" if job_request.script else "scenes"
        if job_request.audio_url:
            request_data["audio_url"] = await _validate_remote_url(
                job_request.audio_url, settings
            )
        request_hash = _fingerprint(request_data)
    elif content_type == "multipart/form-data":
        try:
            form = await request.form(max_files=1, max_fields=32)
        except MultiPartException as exc:
            raise ApiError(400, "INVALID_MULTIPART", "The multipart request is malformed.") from exc
        allowed_fields = {
            "file",
            "audio",
            "voice",
            "aspect_ratios",
            "style",
            "avoid",
            "captions",
            "scenes",
            "script",
            "audio_url",
        }
        unexpected_fields = set(form.keys()) - allowed_fields
        if unexpected_fields:
            raise ApiError(
                422,
                "UNKNOWN_MULTIPART_FIELD",
                "The multipart request contains unsupported fields.",
                details={"fields": sorted(unexpected_fields)},
            )
        upload = form.get("file") or form.get("audio")
        if not isinstance(upload, UploadFile):
            raise ApiError(
                422,
                "MISSING_AUDIO_FILE",
                "Multipart requests must include an audio file in the 'file' field.",
            )
        raw = {
            "voice": str(form.get("voice") or "en-IN-female"),
            "aspect_ratios": _parse_optional_json(form.get("aspect_ratios"), "aspect_ratios")
            or ["16:9"],
            "style": str(form.get("style") or "bright, modern, real-world footage"),
            "avoid": _parse_optional_json(form.get("avoid"), "avoid") or [],
            "captions": _parse_optional_json(form.get("captions"), "captions") or {},
        }
        if any(form.get(field) for field in ("audio_url", "script", "scenes")):
            raise ApiError(
                422,
                "MULTIPLE_INPUTS",
                "An audio upload cannot be combined with audio_url, script, or scenes.",
            )
        filename = Path(upload.filename or "").name
        if filename in {"", ".", ".."}:
            raise ApiError(422, "INVALID_FILENAME", "The uploaded audio filename is invalid.")
        data = bytearray()
        while chunk := await upload.read(64 * 1024):
            data.extend(chunk)
            if len(data) > settings.max_upload_bytes:
                raise ApiError(
                    413,
                    "UPLOAD_TOO_LARGE",
                    "The uploaded audio exceeds the configured upload limit.",
                    details={"max_upload_bytes": settings.max_upload_bytes},
                )
        audio_format = validate_audio_upload(
            filename,
            upload.content_type or "application/octet-stream",
            bytes(data),
            max_upload_bytes=settings.max_upload_bytes,
            max_audio_seconds=settings.max_audio_seconds,
        )
        if form.get("scenes") is not None:
            scenes = _parse_optional_json(form.get("scenes"), "scenes")
            if not isinstance(scenes, list) or len(scenes) > settings.max_scenes:
                raise ApiError(
                    422,
                    "TOO_MANY_SCENES",
                    "The scene list exceeds the configured maximum.",
                    details={"max_scenes": settings.max_scenes},
                )
        options = _parse_multipart_options(raw)
        request_data = options.model_dump(mode="json", exclude_none=True)
        input_type = "audio_upload"
        request_data["upload"] = {
            "filename": filename,
            "format": audio_format,
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        request_hash = _fingerprint(request_data, request_data["upload"]["sha256"])

        existing = None
        with request.app.state.session_factory() as session:
            existing = _existing_job(session, key, request_hash)
        if existing is not None:
            return _accepted(existing)
        destination = settings.input_directory / f"{uuid.uuid4()}.{audio_format}"
        try:
            destination.write_bytes(data)
        except OSError as exc:
            raise ApiError(
                500,
                "INPUT_STORAGE_FAILED",
                "The uploaded audio could not be stored.",
                retryable=True,
            ) from exc
        request_data["upload"]["storage_path"] = str(destination)
    else:
        raise ApiError(
            415,
            "UNSUPPORTED_CONTENT_TYPE",
            "Use application/json or multipart/form-data.",
            details={"supported_content_types": ["application/json", "multipart/form-data"]},
        )

    try:
        job = _create_idempotent_job(
            request,
            key=key,
            request_hash=request_hash,
            input_type=input_type,
            request_data=request_data,
        )
    except ApiError:
        upload_data = request_data.get("upload")
        if upload_data and upload_data.get("storage_path"):
            Path(upload_data["storage_path"]).unlink(missing_ok=True)
        raise
    upload_data = request_data.get("upload")
    if upload_data and upload_data.get("storage_path"):
        if job.request_data["upload"].get("storage_path") != upload_data["storage_path"]:
            Path(upload_data["storage_path"]).unlink(missing_ok=True)
    return _accepted(job)


@router.get(
    "/jobs/{job_id}",
    operation_id="get_job",
    summary="Get job status, progress, warnings, and results",
    response_model=JobStatus,
    responses={
        404: {
            "model": ErrorResponse,
            "description": "The requested job does not exist.",
        },
        422: {"model": ErrorResponse, "description": "The job identifier is invalid."},
    },
)
def get_job(job_id: str, request: Request) -> dict:
    with request.app.state.session_factory() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise ApiError(404, "JOB_NOT_FOUND", "The requested job does not exist.")
        result = {
            "job_id": job.id,
            "status": job.status,
            "progress": job.progress,
            "warnings": job.warnings,
            "result_links": job.result_links,
            "transcript": job.transcript,
            "scene_plan": job.scene_plan,
            "narration_duration_seconds": job.narration_duration_seconds,
            "created_at": job.created_at.isoformat(),
            "updated_at": job.updated_at.isoformat(),
        }
        if job.status == "failed":
            result["error"] = {
                "code": job.error_code,
                "message": job.error_message,
            }
        return result


@router.get(
    "/jobs/{job_id}/artifacts/{artifact_name}",
    operation_id="download_job_artifact",
    summary="Download a validated job artifact",
    responses={
        404: {
            "model": ErrorResponse,
            "description": "The job or requested artifact does not exist.",
        },
    },
)
def download_job_artifact(job_id: str, artifact_name: str, request: Request):
    with request.app.state.session_factory() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise ApiError(404, "JOB_NOT_FOUND", "The requested job does not exist.")
        if job.status != "succeeded" or artifact_name not in {
            Path(link).name for link in job.result_links.values()
        }:
            raise ApiError(
                404,
                "ARTIFACT_NOT_FOUND",
                "The requested job artifact is not available.",
            )
    if (
        Path(artifact_name).name != artifact_name
        or "\\" in artifact_name
        or artifact_name
        not in {
            "captions.srt",
            "timeline.json",
            "manifest.json",
            "video_16_9.mp4",
            "video_9_16.mp4",
            "video_1_1.mp4",
        }
    ):
        raise ApiError(404, "ARTIFACT_NOT_FOUND", "The requested job artifact is not available.")
    output_root = request.app.state.settings.output_directory or (
        request.app.state.settings.input_directory / "outputs"
    )
    artifact_path = output_root / job_id / artifact_name
    if not artifact_path.is_file():
        raise ApiError(404, "ARTIFACT_NOT_FOUND", "The requested job artifact is not available.")
    media_type = (
        "video/mp4"
        if artifact_name.endswith(".mp4")
        else "application/x-subrip"
        if artifact_name.endswith(".srt")
        else "application/json"
    )
    return FileResponse(artifact_path, media_type=media_type, filename=artifact_name)


@router.post(
    "/jobs/{job_id}/ratings",
    status_code=201,
    response_model=SceneRatingResponse,
    operation_id="rate_scene_relevance",
    summary="Record a human relevance rating for a planned scene",
    responses={
        400: {"model": ErrorResponse, "description": "Missing or invalid idempotency key."},
        404: {"model": ErrorResponse, "description": "Job or scene was not found."},
        409: {"model": ErrorResponse, "description": "Idempotency key was reused with different data."},
        422: {"model": ErrorResponse, "description": "Rating payload is invalid."},
    },
)
def rate_scene_relevance(
    job_id: str,
    payload: SceneRatingRequest,
    request: Request,
) -> dict:
    supplied_key = request.headers.get("Idempotency-Key")
    if supplied_key is None:
        raise ApiError(400, "MISSING_IDEMPOTENCY_KEY", "The Idempotency-Key header is required.")
    key = _validate_idempotency_key(supplied_key, request.app.state.settings)
    request_data = payload.model_dump(mode="json")
    request_hash = hashlib.sha256(
        json.dumps(request_data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    with request.app.state.session_factory.begin() as session:
        prior = session.scalar(
            select(SceneRating).where(SceneRating.idempotency_key == key)
        )
        if prior is not None:
            if prior.request_hash != request_hash or prior.job_id != job_id:
                raise ApiError(
                    409,
                    "IDEMPOTENCY_KEY_REUSED",
                    "This Idempotency-Key was already used with a different rating.",
                )
            return _rating_response(prior, idempotent_replay=True)

        job = session.get(Job, job_id)
        if job is None or not job.scene_plan:
            raise ApiError(404, "JOB_NOT_FOUND", "A scene plan for the requested job was not found.")
        scenes = job.scene_plan.get("scenes", [])
        if not any(
            isinstance(scene, dict) and scene.get("scene_id") == payload.scene_id
            for scene in scenes
        ):
            raise ApiError(404, "SCENE_NOT_FOUND", "The requested scene was not found in the job plan.")
        rating = SceneRating(
            job_id=job_id,
            scene_id=payload.scene_id,
            score=payload.score,
            rationale=payload.rationale,
            idempotency_key=key,
            request_hash=request_hash,
        )
        try:
            with session.begin_nested():
                session.add(rating)
                session.flush()
            return _rating_response(rating, idempotent_replay=False)
        except IntegrityError:
            prior = session.scalar(
                select(SceneRating).where(SceneRating.idempotency_key == key)
            )
            if prior is None:
                raise
            if prior.request_hash != request_hash or prior.job_id != job_id:
                raise ApiError(
                    409,
                    "IDEMPOTENCY_KEY_REUSED",
                    "This Idempotency-Key was already used with a different rating.",
                )
            return _rating_response(prior, idempotent_replay=True)


@router.get(
    "/jobs/{job_id}/ratings",
    operation_id="list_scene_relevance_ratings",
    summary="List saved human scene relevance ratings",
    responses={404: {"model": ErrorResponse, "description": "Job was not found."}},
)
def list_scene_ratings(job_id: str, request: Request) -> dict:
    with request.app.state.session_factory() as session:
        if session.get(Job, job_id) is None:
            raise ApiError(404, "JOB_NOT_FOUND", "The requested job does not exist.")
        ratings = session.scalars(
            select(SceneRating)
            .where(SceneRating.job_id == job_id)
            .order_by(SceneRating.created_at, SceneRating.id)
        ).all()
        return {
            "job_id": job_id,
            "ratings": [
                {
                    "rating_id": rating.id,
                    "scene_id": rating.scene_id,
                    "score": rating.score,
                    "rationale": rating.rationale,
                    "created_at": rating.created_at.isoformat(),
                }
                for rating in ratings
            ],
        }


def _rating_response(rating: SceneRating, *, idempotent_replay: bool) -> dict:
    return {
        "rating_id": rating.id,
        "job_id": rating.job_id,
        "scene_id": rating.scene_id,
        "score": rating.score,
        "rationale": rating.rationale,
        "created_at": rating.created_at,
        "idempotent_replay": idempotent_replay,
    }
