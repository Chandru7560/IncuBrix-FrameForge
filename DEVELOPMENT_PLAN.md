# FrameForge Studio Development Plan

## Current verified baseline

The workspace has a FastAPI service, persistent asynchronous jobs, idempotent submissions, script/scene/audio-upload/audio-URL inputs, deterministic contextual scene planning, rights-aware retrieval adapters, an FFmpeg renderer, a local browser studio, a batch CLI, and offline tests.

Latest verification: `python -m pytest tests\unit tests\integration -o addopts= -q -rs` — **128 passed, 0 skipped, 0 failed**. The conditional FFmpeg renderer test ran. Local browser smoke confirmed page/API availability, script submission, honest failure when mock TTS cannot produce narration, and no horizontal overflow at 390 px. Docker and Git are unavailable in the current environment. There is no publishable end-to-end video because live narration and asset-level rights/watermark evidence are not configured.

## Completed implementation slices

1. **Service foundation:** configuration, structured errors/logging, health/readiness, OpenAPI 3.1, persistent SQLite/PostgreSQL jobs, worker lifecycle, and idempotency.
2. **Narrative planning:** script and structured-scene planning, transcript timing, complete coverage checks, mock-provider disclosures, and explicit no-fabrication behavior.
3. **Input and URL handling:** upload and URL validation; URL fetching pins the connection to a validated public IP, validates redirects, enforces bounded size/time/duration, and cleans temporary files.
4. **Visual and media pipeline:** footage adapters, rights/passport filtering, weighted ranking, FFmpeg rendering, output metadata/validation, local artifact links, and scene-segment caching.
5. **Studio and evidence:** API-connected UI, job monitoring, scene inspection, rating/evaluator interfaces, batch runner, test fixtures, traceability, risk, source, AI-use, deployment, and report documentation.

## Remaining work, in dependency order

1. **Configure real speech:** select and approve STT/TTS providers; add provider-specific adapters, credentials via a secret manager, cost/retention settings, and deterministic offline tests. Validate language support and timings.
2. **Prove media rights:** integrate a source that can establish asset-level commercial use, attribution, and no-watermark status. Keep fail-closed behavior until evidence is available.
3. **Complete artifact storage:** implement and test MinIO/S3 uploads and private download delivery; verify persistence, restart behavior, and authorization.
4. **Add scene repair:** define authenticated replacement semantics, preserve provenance, reuse content-addressed segments, and measure partial-versus-full render time.
5. **Validate deployment:** run clean Docker Compose startup, health/readiness, worker recovery, persistence, and full API-to-render flow on a Docker-capable host.
6. **Collect assessment evidence:** run ten varied real inputs, inspect generated videos, obtain human relevance scores, measure cost/latency/memory/concurrency, and complete the workbook truthfully.
7. **Release:** create a clean archive with checksums, arrange user-reviewed GitHub publishing, and upload to an approved Drive destination if available. Do not publish the confidential assessment brief.

## Release gates

- Never mark a planning-only job or synthetic media as a completed video.
- Require real narration, eligible rights-verified footage, and passing output validation for video success.
- Keep all unknown rights, costs, relevance, and performance data explicitly unavailable.
- Re-run the full offline suite and inspect the final diff before creating any release.
- Fill personal details and declaration/signature fields only with the user's own information and confirmation.
