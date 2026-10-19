"""Persistent queue worker and worker-process entry point."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker

from scenepilot.config import Settings
from scenepilot.models import Job, utc_now
from scenepilot.services.narrative_pipeline import NarrativePipelineError, NarrativeScenePipeline

logger = logging.getLogger("scenepilot.worker")

ALLOWED_ARTIFACTS = {
    "captions.srt",
    "timeline.json",
    "manifest.json",
    "video_16_9.mp4",
    "video_9_16.mp4",
    "video_1_1.mp4",
}


class ArtifactValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class JobWorker:
    def __init__(
        self,
        session_factory: sessionmaker,
        settings: Settings,
        *,
        provider: NarrativeScenePipeline | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.provider = provider or NarrativeScenePipeline(settings)

    def run_once(self) -> bool:
        with self.session_factory() as session:
            job = session.scalar(
                select(Job).where(Job.status == "queued").order_by(Job.created_at, Job.id).limit(1)
            )
            if job is None:
                return False
            job_id = job.id
            claimed = session.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == "queued")
                .values(status="running", progress=10, updated_at=utc_now())
            )
            if claimed.rowcount != 1:
                session.rollback()
                return False
            session.commit()
            input_type = job.input_type
            request_data = dict(job.request_data)
            request_data["_job_id"] = job_id
            correlation_id = job.correlation_id

        logger.info(
            "job_started",
            extra={"job_id": job_id, "correlation_id": correlation_id},
        )
        try:
            result = self.provider.process(input_type, request_data)
        except NarrativePipelineError as exc:
            logger.warning(
                "job_processing_rejected",
                extra={
                    "job_id": job_id,
                    "correlation_id": correlation_id,
                    "error_code": exc.code,
                },
            )
            self._mark_failed(
                job_id,
                error_code=exc.code,
                error_message=exc.message,
                warnings=exc.warnings,
            )
            return True
        except Exception as exc:
            logger.error(
                "job_failed",
                extra={
                    "job_id": job_id,
                    "correlation_id": correlation_id,
                    "error_type": type(exc).__name__,
                },
            )
            self._mark_failed(
                job_id,
                error_code="JOB_PROCESSING_FAILED",
                error_message="The job could not be processed.",
                warnings=[
                    "Processing failed unexpectedly; see sanitized service logs for "
                    f"the exception type ({type(exc).__name__})."
                ],
            )
            return True

        try:
            result_links = self._validate_result_artifacts(job_id, result)
        except ArtifactValidationError as exc:
            logger.error(
                "job_artifact_validation_failed",
                extra={
                    "job_id": job_id,
                    "correlation_id": correlation_id,
                    "error_code": exc.code,
                },
            )
            self._mark_failed(
                job_id,
                error_code=exc.code,
                error_message=str(exc),
                warnings=["The worker rejected the renderer's incomplete or unsafe output."],
            )
            return True

        with self.session_factory() as session:
            session.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == "running")
                .values(
                    status="succeeded",
                    progress=100,
                    warnings=result["warnings"],
                    result_links=result_links,
                    transcript=result.get("transcript"),
                    scene_plan=result.get("scene_plan"),
                    narration_duration_seconds=result.get("narration_duration_seconds"),
                    updated_at=utc_now(),
                )
            )
            session.commit()
        logger.info(
            "job_succeeded",
            extra={"job_id": job_id, "correlation_id": correlation_id},
        )
        return True

    def _validate_result_artifacts(self, job_id: str, result: object) -> dict[str, str]:
        if not isinstance(result, dict):
            raise ArtifactValidationError(
                "INVALID_RENDER_RESULT",
                "The worker received an invalid processing result.",
            )
        warnings = result.get("warnings", [])
        if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
            raise ArtifactValidationError(
                "INVALID_RENDER_RESULT",
                "The worker received invalid processing warnings.",
            )
        artifact_names = result.get("artifact_names", [])
        if not isinstance(artifact_names, list):
            raise ArtifactValidationError(
                "INVALID_RENDER_RESULT",
                "The renderer returned an invalid artifact list.",
            )
        if not artifact_names and self.settings.render_enabled:
            raise ArtifactValidationError(
                "RENDERED_OUTPUT_MISSING",
                "A video job cannot succeed without a validated MP4 output.",
            )

        output_root = self.settings.output_directory or (
            self.settings.input_directory / "outputs"
        )
        job_output_directory = output_root / job_id
        result_links = {}
        for name in artifact_names:
            if (
                not isinstance(name, str)
                or name not in ALLOWED_ARTIFACTS
                or Path(name).name != name
                or "\\" in name
            ):
                raise ArtifactValidationError(
                    "INVALID_RENDER_ARTIFACT",
                    "The renderer returned an unsafe or unsupported artifact name.",
                )
            artifact_path = job_output_directory / name
            if not artifact_path.is_file():
                raise ArtifactValidationError(
                    "RENDERED_ARTIFACT_MISSING",
                    "The renderer reported an artifact that is not present on disk.",
                )
            try:
                artifact_path.resolve(strict=True).relative_to(
                    job_output_directory.resolve(strict=True)
                )
            except (OSError, ValueError) as exc:
                raise ArtifactValidationError(
                    "INVALID_RENDER_ARTIFACT",
                    "The renderer returned an artifact outside the job output directory.",
                ) from exc
            key = (
                "video_"
                + name.removeprefix("video_").removesuffix(".mp4").replace("_", ":")
                if name.startswith("video_")
                else name
            )
            result_links[key] = (
                f"/api/v1/jobs/{job_id}/artifacts/{quote(name, safe='._-')}"
            )
        return result_links

    def _mark_failed(
        self,
        job_id: str,
        *,
        error_code: str,
        error_message: str,
        warnings: list[str],
    ) -> None:
        with self.session_factory() as session:
            session.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == "running")
                .values(
                    status="failed",
                    progress=100,
                    error_code=error_code,
                    error_message=error_message,
                    warnings=warnings,
                    updated_at=utc_now(),
                )
            )
            session.commit()

    def run_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            if not self.run_once():
                stop_event.wait(self.settings.worker_poll_seconds)


def serve_worker() -> None:
    from scenepilot.config import ensure_input_directory
    from scenepilot.db import create_database, initialize_database

    settings = Settings.from_env()
    ensure_input_directory(settings)
    engine, session_factory = create_database(settings.database_url)
    initialize_database(engine)
    worker = JobWorker(session_factory, settings)
    stop_event = threading.Event()
    try:
        worker.run_forever(stop_event)
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        engine.dispose()


if __name__ == "__main__":
    serve_worker()
