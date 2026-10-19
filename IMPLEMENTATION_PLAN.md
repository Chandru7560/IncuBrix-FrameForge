# ScenePilot AI Implementation Plan

This checklist is the working execution record. An item remains unchecked until its acceptance criteria have executable evidence; design notes or the presence of a file alone do not count as feature completion.

## Stage 1 — workspace inspection and traceability

- [x] Inventory existing files, dependencies/configuration/tests/assets (workspace inspection; there was no implementation to preserve).
- [x] Read all seven pages of the assessment brief.
- [x] Inspect the contents of all eight submission workbook worksheets.
- [x] Create the initial requirements, architecture, implementation-plan, and risk documents.
- [x] Record local tool constraints (Docker, Git, and GitHub CLI unavailable; FFmpeg/FFprobe installed for media validation; no local `.git` directory).

Acceptance: inspection baseline is accurate, every assessment must-have/level/integration item maps to planned files and executable tests, and no feature is represented as complete without evidence.

## Stage 2 — project foundation and runnable local environment

- [x] Create the Python package, dependency manifest, configuration validation, structured errors/logging, and repository hygiene.
- [x] Add Dockerfile and Compose services for API, worker, PostgreSQL, and MinIO; document all environment settings in `.env.example`.
- [x] Add deterministic mock provider interfaces and a no-key unit-test command.
- [x] Add `/health`, `/ready`, versioned API skeleton, and OpenAPI 3.1 contract checks.
- [x] Add persistent job/idempotency records, POST request validation, safe URL policy, upload limits, progress/status output, and concurrent retry tests.

Acceptance: the unit/integration tests pass locally without provider keys and prove the OpenAPI contract matches FastAPI. Compose files and Dockerfile are present, but clean container startup/readiness has not been verified because Docker is unavailable. Mock jobs deliberately generate no video.

Local runtime behavior: `RENDER_ENABLED` defaults to `false`, so the no-provider app returns disclosed scene plans instead of failing every script job on the silent mock TTS fixture. Rendering is an explicit opt-in and remains fail-closed.

## Stage 3 — validated inputs, durable jobs, idempotency

- [x] Implement script, scene-list, audio-upload, and audio-URL request modes and validation, including MP3/WAV/M4A and 10-minute limit.
- [x] Download audio URLs only after public HTTPS validation; pin the connection to a resolved public IP, validate redirects, enforce byte/time/duration bounds, and remove partial temporary downloads.
- [x] Persist job state; implement background worker lifecycle and progress reporting.
- [x] Implement durable `Idempotency-Key` handling for every POST, including concurrent requests and key/request conflicts.
- [x] Add consistent structured error responses.
- [ ] Add signed optional callbacks.

Acceptance: API and pipeline tests prove request acceptance, injected URL-fetch processing, explicit validation failures, 202/job GET behavior, progress/state transitions, and same-key single execution. URL speech recognition still requires a real adapter; signed callbacks remain unimplemented.

## Stage 4 — narration, scene intelligence, rights-aware selection

- [x] Define injectable speech-recognition/TTS contracts and deterministic mock behavior. The mock recognizer refuses to fabricate speech; mock TTS creates a disclosed silent timing fixture. No live provider is wired into worker startup.
- [x] Add audio probing, transcript preservation/warnings, generated-audio duration measurement, phrase-boundary segmentation, short-phrase merging, long-phrase splitting, and contiguous full-duration coverage validation.
- [x] Add deterministic contextual scene/visual-hint interpretation for the requested ambiguous meanings and persist readable scene plans.
- [x] Add configurable Pexels and Pixabay adapters, source-order failover, deterministic local non-publishable fixtures, and asset filtering/ranking.
- [x] Record candidate/provider responses, rejected alternatives, source attempts, score components, and per-scene Rights Passports. Unverified rights or watermark status fail closed.
- [x] Add regression tests for cloud/weather, financial/abstract value, animal/device mouse, financial/river bank, abstract trust, and numerical statistics.

Acceptance evidence: the latest unit/integration run passed **128 tests**, with no skips; the FFmpeg/FFprobe-dependent render test ran. `tests/unit/test_visual_retrieval.py` and `tests/unit/test_footage_download.py`, plus narrative tests, cover provider switching, outages, empty results, duplicates, unverified rights, watermark rejection, corrupted downloads, contextual intent, and timeline coverage. Live speech providers and independently verified provider-asset rights/watermark evidence remain unavailable.

## Stage 5 — rendering, storage, outputs

- [x] Implement FFmpeg-based scene rendering for requested ratios, H.264/AAC, 30 fps, 1080p-class output, crop/reframe, normalization, narration, optional music, and captions.
- [x] Generate `captions.srt`, `timeline.json`, and `manifest.json` with rights, provider/version, timing, cost availability, processing statistics, warnings, and quality-check results.
- [x] Persist output files and expose allowlisted artifact-download links only after job success; output storage is a shared local volume, not S3.
- [x] Validate stream properties, file decode integrity, duration/timeline coverage, duplicate clips, black/frozen frames where measurable, and report unavailable checks.
- [ ] Verify clean Docker/Compose startup and S3-compatible artifact storage.

Acceptance evidence: the renderer implementation has conditional tests for 16:9, 9:16, and 1:1 outputs and scene-cache reuse. The final complete test run passed **129 tests**, including artifact-route and failure protections; its real-binary FFmpeg integration test passed. Synthetic rights fixtures do not demonstrate publishable asset retrieval.

## Stage 6 — resilience and advanced capabilities

- [x] Add alternate-source selection and explicit failure/fallback reporting. Provider-level retry policy remains bounded by adapter behavior.
- [ ] Add scene clip replacement and affected-segment-only rerender with measured comparison.
- [x] Add minimum/maximum scene-duration rules and FFmpeg signalstats continuity diagnostics; no perceptual cross-source colour guarantee is claimed.
- [x] Add an optional independent visual-language evaluator, persisted human ratings, and an agreement report utility. The evaluator is disabled by default; no real scores or reviewer agreement are available.
- [ ] Validate Hindi/Tamil narration with configured, commercially suitable providers.
- [ ] Measure controlled cold/warm turnaround, cost per finished minute, peak memory, and max concurrent production jobs.
- [x] Record a measured per-render elapsed time, validated plan coverage, cached-segment ratio, duplicate-clip rate, and actual evaluator scores when available. Unknown operational metrics are explicitly represented as unavailable in manifests.

Acceptance: provider-failure, media diagnostics, evaluator behavior, and rating-report calculation have executable tests. Scene replacement, multilingual speech, production relevance agreement, and controlled operational benchmarks remain Partial/No.

## Stage 7 — evidence, integration, and submission readiness

- [x] Implement and unit-test the CLI batch runner for a single file or recursive folder.
- [x] Add ten varied request fixtures and document which are fixtures versus actual executable tests/rendered outputs.
- [x] Update setup, architecture, requirements traceability, risk, source, AI-use, evidence, deployment, and report documentation.
- [ ] Capture a retained live batch run with actual narration and rights-verified footage; none is available in this environment.
- [ ] Verify all ten Integration Standard requirements and the full must-have suite from a clean Docker Compose startup.
- [x] Create an unsigned technical workbook draft with evidence-backed statuses and measured test results; leave personal details and declarations for the owner.
- [ ] Publish to GitHub and copy the archive to an approved Drive destination after confirming authenticated tools and repository/folder access.

Acceptance: tests and evidence are reproducible from documented commands; no simulated result, benchmark, licence, provider status, demo, or claim is described as real.

## Current status and next step

The core API, planning, retrieval, real-file renderer, local artifact path, studio, optional evaluator, rating persistence, batch CLI, and evidence documents are implemented. Local runtime defaults to disclosed scene planning; mock narration is silent, and uploaded-audio/URL-audio recognition require an injected adapter. Audio URLs are downloaded with public-IP pinning, redirect checks, byte/time limits, format/duration validation, and temporary-file cleanup. Pexels/Pixabay search adapters exist, but current provider metadata cannot satisfy watermark verification, and no credentials or publishable assets are configured. Scene-segment caching is implemented; scene replacement, S3 uploads, live speech adapters, and clean Compose acceptance remain incomplete. See `REPORT.md` for the latest actual test run and exact release checklist. Do not claim a publishable end-to-end result without eligible media and narration.
