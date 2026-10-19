"""FastAPI application factory and ASGI entry point."""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from scenepilot.api.middleware import CorrelationAndSizeMiddleware
from scenepilot.api.routes.health import router as health_router
from scenepilot.api.routes.jobs import router as jobs_router
from scenepilot.config import Settings, ensure_input_directory
from scenepilot.db import create_database, initialize_database
from scenepilot.errors import install_error_handlers
from scenepilot.observability import configure_logging
from scenepilot.services.narrative_pipeline import NarrativeScenePipeline
from scenepilot.worker import JobWorker

logger = logging.getLogger("scenepilot")


def create_app(
    settings: Settings | None = None,
    *,
    job_provider: NarrativeScenePipeline | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)
    ensure_input_directory(settings)
    engine, session_factory = create_database(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        initialize_database(engine)
        stop_event = threading.Event()
        worker_thread = None
        if settings.worker_enabled:
            worker = JobWorker(
                session_factory,
                settings,
                provider=app.state.job_provider,
            )
            worker_thread = threading.Thread(
                target=worker.run_forever,
                args=(stop_event,),
                name="scenepilot-worker",
                daemon=True,
            )
            worker_thread.start()
        logger.info("application_started", extra={"job_id": None})
        try:
            yield
        finally:
            stop_event.set()
            if worker_thread is not None:
                worker_thread.join(timeout=5)
            engine.dispose()
            logger.info("application_stopped", extra={"job_id": None})

    app = FastAPI(
        title="ScenePilot AI API",
        version="0.1.0",
        description=(
            "Asynchronous API foundation for narration-to-B-roll jobs. "
            "Mock mode validates job handling but does not generate media."
        ),
        openapi_version="3.1.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.job_provider = job_provider or NarrativeScenePipeline(settings)
    app.add_middleware(
        CorrelationAndSizeMiddleware,
        max_request_bytes=settings.max_request_bytes,
    )
    install_error_handlers(app)
    app.include_router(health_router)
    app.include_router(jobs_router)
    studio_directory = Path(__file__).parent / "studio"
    app.mount(
        "/studio-assets",
        StaticFiles(directory=studio_directory, check_dir=True),
        name="studio-assets",
    )

    @app.get("/", include_in_schema=False)
    def frameforge_studio():
        return FileResponse(studio_directory / "index.html")

    return app


app = create_app()
