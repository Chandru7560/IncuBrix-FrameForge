# FrameForge Studio assessment report

## Executive summary

This repository contains a modular FastAPI job service, contextual scene planner, configurable footage adapters with fail-closed rights checks, an FFmpeg renderer, a browser-based editing workspace, persistent scene ratings, an optional independent visual evaluator, and a folder-capable batch CLI.

The core is **not a complete publishable video engine yet**. No working live speech-recognition or narration provider is wired into the worker. The deterministic mock TTS produces a silent timing fixture; the default recognizer refuses to invent a transcript. The configured footage adapters do not provide the per-asset commercial-rights and watermark evidence required by the renderer. Therefore, this environment cannot demonstrate a real narrated video using publishable footage. The system reports planning-only/failure states rather than presenting fabricated media as successful output.

## Design alternatives and decisions

- **Durable background polling worker vs request-bound processing:** jobs are persisted in SQLAlchemy-backed SQLite for local development and PostgreSQL in Compose. POST is idempotent and returns a job handle; processing does not run in the request handler.
- **Deterministic domain fallback vs implicit AI fallback:** semantic scene interpretation works offline with explicit fallback reasons. Speech and footage are not fabricated by the fallback.
- **Stock-provider first vs generative fallback:** Pexels/Pixabay can be queried in configured order and candidates are filtered before ranking. No generative fallback is configured. Missing rights/watermark evidence disqualifies an asset.
- **FFmpeg scene-segment cache vs full-frame cache:** content-addressed, ratio-specific scene segments are reused; final assembly remains per-render. This is a caching foundation, not a scene-replacement API or measured partial-render workflow.
- **Local filesystem vs S3-compatible output store:** local persisted artifacts are implemented. MinIO is provisioned in Compose, but S3 uploads/download links are not wired; this remains a release blocker for the stated storage requirement.
- **Optional external vision-language evaluator vs heuristic-only score:** the optional OpenAI-compatible adapter evaluates sampled frames from the selected footage alongside narration/intent. It is disabled by default and never substitutes a heuristic score when unavailable. External privacy/terms review is required.

## Verified implementation

- Versioned API creation/status/artifact/rating surfaces, health/readiness, validation, structured errors/logs, job persistence, and idempotency.
- Scene planning with phrase-aware boundaries and full-duration coverage; input/transcript warnings and safe provider-failure behavior.
- Two independent optional footage-source adapters, deterministic synthetic offline metadata adapter, score policy, source failover, Rights Passports, and fail-closed commercial-use/watermark checks.
- Actual FFmpeg H.264/AAC rendering implementation with captions, timeline, manifest, 30 fps ratio-specific outputs, file validation, content-addressed segment cache, duplicate/black/freeze checks, and signalstats continuity diagnostics. Real renderer tests use synthetic generated test patterns and tone, not licensed footage or speech.
- FrameForge Studio frontend interactions connected to implemented API actions, with job polling guarded against overlapping submissions and stale status responses. Scene replacement is explicitly unavailable. No fabricated recent jobs or success scores are shown.
- Persistent human rating endpoints and report calculation; no reviewer data is present.
- Recursive batch runner preserves terminal failures and downloads only real API-linked artifacts.
- Ten request fixtures in `testpack/cases/`; fixture targets/metadata are not actual media durations or output claims.

### Measured renderer manifest fields

The manifest stores the elapsed time for the specific renderer invocation, validated scene-plan coverage, repeat-clip rate, cache hit/miss statistics, continuity profiles, and independent evaluation records when available. Cold/warm end-to-end turnaround, cost per finished minute, peak memory, and production maximum concurrency are explicitly marked unavailable. A per-render measurement is not a benchmark.

## Validation record

The final full run `python -m pytest tests\unit tests\integration -o addopts= -q -rs` completed with **129 passed, 0 skipped, 0 failed**, exit code 0, in 24.56 seconds. FFmpeg/FFprobe were available, so the conditional real-render test executed. Pytest reported one Starlette deprecation warning; pytest-asyncio also emitted a loop-scope configuration deprecation at startup. The output is saved in `testpack/results/full-regression.txt`; run metadata and warning disclosure are in `testpack/results/test-index.json`. Docker remains unavailable for image/deployment verification.

The latest integrated browser smoke test verified the actual service page, API health/readiness, and script submission in the default local configuration. The job successfully returned a one-scene plan with explicit mock-audio and no-eligible-footage warnings; rendering was disabled, so it returned no video artifacts. At a 390-pixel viewport the layout had no horizontal overflow. A separate overlap test held one job's status response while submitting a second job and confirmed the newer job reached success and remained selected when the old response was released. Raw details are in `testpack/results/browser-smoke.json`; an earlier 2026-10-17 smoke run is retained there as historical evidence.

The renderer test exercised landscape, portrait, and square encodes, stream metadata, captions/timeline/manifest existence, content-addressed scene cache reuse, and measurable continuity data. Synthetic pattern and tone sources used by the test are technical signals only, not stock footage, narration, or a production video.

## Ten Integration Standard points

| # | Status | Evidence / limitation |
|---|---|---|
| 1. One-command Docker Compose startup and one-command no-key testing | **Partial** | `docker-compose.yml`, `.env.example`, and pytest command exist. Docker/Compose startup could not be run in this environment. |
| 2. OpenAPI 3.1 synchronized and business routes under `/api/v1/` | **Implemented locally** | `openapi.yaml` is checked against FastAPI's generated schema in integration tests; operational `/health` and `/ready` remain unversioned as specified. |
| 3. Background jobs and progress | **Implemented locally** | Persistent queue/status lifecycle and progress tests. |
| 4. Safe idempotent retries | **Implemented locally** | Durable same-key replay, conflict, restart, and concurrent submission tests. |
| 5. Consistent structured JSON errors | **Implemented locally** | Common error shape and validation/storage/provider failure tests. |
| 6. Environment-driven configuration | **Implemented locally** | Validated settings and `.env.example`; Compose placeholder passwords are local-only. |
| 7. Upload/URL inputs and S3-compatible output storage/download links | **Partial** | Upload and local artifact downloads exist. URL audio is fetched through a pinned public IP with bounds, redirect validation, and audio format/duration checks, then sent to the same speech adapter; live STT is not configured. S3/MinIO persistence is not implemented. |
| 8. JSON logs, correlation IDs, health and readiness | **Implemented locally** | Middleware/logging/health tests; no deployed endpoint was available. |
| 9. Complete output manifests | **Partial** | Manifest includes providers/configuration/seed/timing/cost-unavailable state/warnings/rights/processing statistics and explicit unmeasured production metrics. Real provider cost data is absent. |
| 10. Batch testing runner | **Implemented locally** | CLI and unit tests cover file/folder discovery, job submission/polling, result capture and failure retention. No real production batch was run. |

## Assessment progress and blockers

| Area | Status |
|---|---|
| API, job lifecycle, idempotency, validation, logging, health, OpenAPI | Implemented and locally tested |
| Narrative Intent Graph and deterministic scene planning | Implemented and tested; live STT/TTS absent |
| Rights-aware source adapters and provenance | Interfaces/policy implemented; real source metadata is insufficient for publishable asset verification |
| FFmpeg renderer | Implemented and technically tested on generated fixtures; publishable end-to-end run unavailable |
| FrameForge Studio | Implemented and API-connected; requires browser verification against the local service; replacement workflow unavailable |
| Advanced independent relevance evaluation | Optional adapter/ratings/report implemented; no credentials or ratings, so no relevance/agreement claim |
| Scene replacement/partial render comparison | Not implemented |
| Tamil/Hindi speech | Not implemented; text request fixtures only |
| 10 completed videos and human 3.5/5 or 85% ratings threshold | Not achieved or claimed |
| Production cold/warm, cost, peak memory, concurrency benchmarks | Not measured |
| S3/MinIO artifact persistence | Not implemented |
| Clean Docker Compose deployment and live demo | Not verified; Docker unavailable |

## Release checklist

### Assessment must-haves

| Must-have | Status |
|---|---|
| Audio upload/URL, script with narration, and structured scene-list intake | **Partial** — uploads and bounded URL fetching/validation are implemented, but live speech generation/transcription is not wired. |
| Meaning-first phrase-aware scenes and full narration timeline | **Implemented for planning** — deterministic planner/tests; no live speech timing. |
| Source/licence/attribution/AI disclosure and no-watermark publishing | **Partial/blocking** — Rights Passport and fail-closed checks exist; no eligible real asset evidence is configured. |
| MP4 for each ratio with normalized media, captions, and integrity checks | **Renderer implemented; acceptance partial** — real source inputs unavailable and the final real-render test skipped without FFmpeg/FFprobe. |
| No repeated clips and contextual selection | **Implemented policy/tests** — synthetic fixtures only; no real clip selection evidence. |
| Safe handling for named entities, brands, and sensitive events | **Partial** — avoid/context rules; no complete independent safety classifier or human approval process. |
| Durable asynchronous jobs, progress, and failure resilience | **Implemented locally** — no deployed worker/load evidence. |
| Integration and maintained demo availability | **Partial/not deployed** — local API works; Docker and public demo are unverified. |

### Advanced and Outstanding

| Capability | Status |
|---|---|
| Two footage adapters and measurable weighted source policy | **Implemented/tested offline; real providers not run or rights-cleared.** |
| Scene replacement and measured partial-render advantage | **Not implemented.** |
| Continuity diagnostics and minimum scene pacing | **Implemented with measurable technical profiles; subjective/perceptual quality remains unverified.** |
| Independent VLM relevance against selected frames and human agreement | **Optional evaluator/rating/report code implemented; no external scores or reviewer sample.** |
| Hindi and Tamil transcription/narration | **Not implemented; fixtures contain text only.** |
| Cold/warm speed, cost/minute, coverage, relevance, reliability, repeat rate, memory, concurrency | **Partial** — per-render elapsed, coverage, cache, duplicate rate, and available evaluator scores recorded; controlled cold/warm, cost, memory, reliability cohort, and concurrency remain unmeasured. |
| Safe fallback without fabricated media or success | **Implemented and exercised** — silent mock narration fails rendering; planning-only jobs are labelled; rights filter fails closed. |
| Ten completed videos and required human relevance thresholds | **Not achieved; no claim made.** |

### Test and deployment

- [x] Offline full suite: **128 passed, 0 skipped, 0 failed** (exit 0); the real FFmpeg render test executed.
- [x] Local browser smoke: API online, health/readiness, script submission, explicit mock-narration failure, and 390 px overflow check.
- [x] FFmpeg real-render test on this run: **passed** using generated technical fixtures.
- [ ] Docker Compose clean-environment startup: **not run** (Docker unavailable).
- [ ] Publishable end-to-end video, S3 persistence, and public deployment: **not verified/available**.

## Future work, in dependency order

1. Select an approved STT/TTS provider, configure credentials through a secret manager, and validate audio retention, language support, timestamps, costs, and terms.
2. Integrate asset-level commercial-rights and no-watermark evidence with a licensed footage source; otherwise continue fail-closed behavior.
3. Implement S3-compatible output storage and signed/private download links, then verify MinIO locally.
4. Add authenticated scene-replacement API semantics and benchmark partial render versus a controlled full render.
5. Run ten varied productions, collect human scene scores, validate Tamil/Hindi providers, and measure cost, throughput, memory, and concurrency on stated hardware.
6. Validate clean Docker Compose startup, service health, persistence, restart behavior, and a complete playable output journey.
