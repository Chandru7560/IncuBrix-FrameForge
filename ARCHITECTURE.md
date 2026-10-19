# ScenePilot AI Architecture

## Status and design goal

The initial workspace contained only the supplied assessment PDF and submission workbook. The current implementation provides the modular FastAPI API, validated inputs, database-backed idempotency, polling worker, structured errors/logging, health/readiness, OpenAPI 3.1, a deterministic narrative-intent planner, rights-aware footage search/download interfaces, an FFmpeg renderer, and a persisted artifact-download route. Live STT/TTS providers, provider credentials, verified stock-asset watermark evidence, external object storage, and container acceptance remain unavailable. Mock narration and local metadata fixtures are deliberately non-publishable.

ScenePilot AI will accept a script, an IncuBrix scene list, or an uploaded/remote voiceover and produce narration-synchronized B-roll outputs with captions, a scene-level provenance timeline, and a manifest. The implementation is designed around explainable interfaces, deterministic local tests, and explicit rights/safety uncertainty.

The current browser workspace is FrameForge Studio (`src/scenepilot/studio/`). It consumes the existing job, scene-plan, rating, and artifact APIs. It does not implement scene replacement, and does not synthesize evidence or recent-project history.

## Proposed technology and deployment

- **API and contracts:** Python 3.12, FastAPI, Pydantic, and OpenAPI 3.1.
- **Persistent state and queue:** SQLAlchemy job and idempotency records, using SQLite for local development and PostgreSQL in Docker Compose. A separately running polling worker claims queued jobs; rendering never runs in the request handler.
- **Artifact storage:** Rendered files are written under configurable output/cache directories shared by API and worker in Compose. MinIO is provisioned locally, but S3-compatible persistence and signed URLs are not implemented.
- **Media pipeline:** FFmpeg/ffprobe in the worker image for probing, scene segment rendering, subtitles, audio mixing, normalization, and output validation. The real render integration test exercises installed binaries when available; Docker deployment remains unverified.
- **Tests:** pytest unit, integration, and end-to-end suites, all runnable with deterministic mock providers and no API keys.
- **Configuration:** validated environment-backed settings in `src/scenepilot/config.py`; `.env.example` lists settings without secrets.
- **Architecture decisions:** see [`docs/adr/0001-persistent-worker.md`](./docs/adr/0001-persistent-worker.md), [`docs/adr/0002-fail-closed-media.md`](./docs/adr/0002-fail-closed-media.md), and [`docs/adr/0003-local-artifact-storage.md`](./docs/adr/0003-local-artifact-storage.md).

## Component boundaries

```text
Client / batch runner
        |
        v
FastAPI (/api/v1/*) -- validation, correlation ID, idempotency
        |                         |
        v                         v
PostgreSQL job ledger       S3-compatible object storage
        |
        v
Worker: input validation/probe -> injected STT or TTS adapter
      -> contextual intent graph -> contiguous narration timeline
      -> safety/rights-aware asset selection -> timeline/captions
      -> FFmpeg rendering -> media validation -> result/manifest storage
        |
        +-- transcription, TTS, scene, footage, relevance provider adapters
            deterministic mock planner; speech/TTS protocols support injection
```

## Request and job lifecycle

1. `POST /api/v1/broll/jobs` validates exactly one input mode (audio upload/URL, script, or supplied scenes), supported formats and ratios, optional captions/style/avoid settings, and callback fields. Audio probing/processing is deferred to the worker; uploads and URL retrieval have configured size, duration, timeout, and redirect limits.
2. The request's `Idempotency-Key` and normalized request fingerprint are stored transactionally with the new job. A repeated key and identical request returns the same job. A key reused for a different request returns a structured conflict; it must not queue duplicate work.
3. The API responds `202 Accepted` with a stable `job_id`, status URL, and correlation ID. Worker state progresses through `queued`, `running`, and either `succeeded` or `failed`, with progress from 0 to 100.
4. **Narrative planning behavior:** scripts and structured scenes are normalized, passed through an injected TTS adapter, and timed from the measured generated WAV. The built-in mock TTS produces a disclosed silent timing fixture only. Uploaded audio and bounded, validated URL downloads are probed, then passed to an injected recognizer; the default mock recognizer refuses to invent speech. Provider timestamps, transcript warnings, original text, and measured source duration are retained.
5. Sentence boundaries, recognition gaps, minimum/maximum scene durations, and structured scene ordering guide scene segmentation. Deterministic contextual rules produce intent, concept, entities/actions, metaphor, mood/sensitivity, avoid constraints, shot/composition/continuity, and hint alignment. Provider failures record fallback reasons; the full timeline is validated for contiguous coverage within the configured tolerance. See [`NARRATIVE_INTENT_GRAPH.md`](./NARRATIVE_INTENT_GRAPH.md).
6. **Footage retrieval behavior:** construct a contextual query from the scene intent and non-conflicting visual hint; search configured Pexels/Pixabay adapters in configured order; reject candidates that fail rights, watermark, safety, avoid, aspect, duration, duplicate, or relevance checks; and try the next candidate/source. The deterministic local adapter emits synthetic non-publishable metadata only.
7. **Rendering behavior:** require real narration and cleared footage; build and content-hash per-scene ratio-specific segments; assemble a contiguous timeline, SRT, MP4s, timeline and manifest; validate outputs before job success; and serve allowlisted files only for succeeded jobs. Files reside on a shared local output volume; S3 uploads are not implemented.
8. Signed callbacks are a planned integration feature and are intentionally not exposed by the current API until delivery and signature handling are implemented.
9. Persisted human scene ratings use their own idempotency records. The optional VLM evaluator samples the selected footage itself, records model/version/normalization/rationale, and returns an unavailable score with a warning when disabled or failing. The agreement CLI computes metrics only from stored ratings; no actual reviewer cohort is present.

## API namespace and operational surfaces

- Business routes live under `/api/v1/`, including `POST /api/v1/broll/jobs`, `GET /api/v1/jobs/{job_id}`, and a planned scene-clip replacement endpoint if partial rerender is implemented.
- `GET /api/v1/jobs/{job_id}/artifacts/{artifact_name}` serves persisted artifacts after successful job completion; it does not proxy to MinIO.
- `GET /api/v1/jobs/{job_id}/ratings` and `POST /api/v1/jobs/{job_id}/ratings` list and persist human scene scores; ratings are not authenticated in the current API.
- The assessment explicitly requires `GET /health` and `GET /ready`; these operational probes are the only planned HTTP endpoints outside the versioned business API.
- `openapi.yaml` is the source-controlled OpenAPI 3.1 contract and will be checked against the live FastAPI schema and route inventory.
- All API failures use `{"error":{"code":"UPPER_SNAKE_CASE","message":"...","retryable":false,"details":{...}}}`.
- Middleware generates or validates `X-Correlation-Id`. Structured JSON events include timestamp, level, correlation ID, and, whenever applicable, `job_id`. Secrets and signed callback payloads are excluded from logs.

## Provider and rights model

Small adapters isolate transcription, text-to-speech, semantic scene interpretation, footage search/generation, and independent relevance scoring. The speech and TTS protocols can be injected into `NarrativeScenePipeline`, but worker startup currently wires only the deterministic mock adapters. Mock speech recognition never fabricates a transcript; mock TTS generates a measured silent timing fixture with an explicit warning. The deterministic semantic planner runs without keys and records fallback reasons. It must never claim a mock still or rendered pattern is genuine footage.

Footage selection uses configured source order and a normalized weighted score: semantic relevance 0.35, action match 0.15, subject match 0.15, composition 0.10, shot variety 0.10, and technical suitability 0.15. The default minimum relevance is 0.35; aspect mismatch must remain within the configured 0.35 tolerance. All weights, source order, threshold, and candidate/download limits are environment-configurable. The selector filters first for rights status, evidence, verified watermark clearance, content-safety status, avoid constraints, fit, and prior selections; it then ranks the remaining candidates and falls through to the next candidate/source. Pexels/Pixabay search results do not prove commercial rights or watermark absence. Their explicit operator approval is only an attestation to reviewed terms, not asset-level watermark evidence. Until an adapter provides eligible evidence, such results remain rejected. Every attempt, candidate rejection, selected score, provider response, rights passport, and rationale are retained in the scene result.

Provider credentials come exclusively from runtime environment/secrets. `SOURCES.md` records actual adapters, upstream terms, runtime dependencies, and unresolved rights evidence. No real paid provider, dataset, model, or production media asset is claimed to have been used.

## Output contract

Each successful job includes:

- `video_{ratio}.mp4` for every requested ratio, encoded to the configured H.264/AAC, 30 fps, 1080p-class profile.
- `captions.srt`, timed against narration; burn-in follows the request.
- `timeline.json`, with narration segments and times, selected clip and source, licence/attribution evidence, AI-generated flag, selection reason and relevance.
- `manifest.json`, with provider/model versions, settings, deterministic seed where applicable, timings, cost estimate or explicit unavailable value, warnings, and rights information.

The manifest includes actual per-invocation renderer elapsed time, validated timeline coverage, duplicate-asset rate, segment-cache statistics, continuity diagnostics, and evaluator outputs when present. Controlled cold/warm end-to-end timing, per-video-minute cost, peak memory, and production concurrency remain explicitly not measured.

The renderer normalizes output to H.264/AAC, 30 fps, yuv420p, and 48 kHz stereo, and creates separate 1920x1080, 1080x1920, or 1080x1080 targets. It uses deterministic center cropping unless an injected subject detector supplies a focal point. Short clips are extended only by bounded motion-compensated slow motion; excessive shortage fails. It validates media streams, duration, decode integrity, timeline coverage, duplicate clips, black frames, and long freezes; unavailable quality checks are marked unavailable in the manifest. It does not claim a reliable automated measure of semantic framing, perceived brightness consistency, or audio intelligibility.

## Security and resilience decisions

- Audio URL validation constrains HTTPS scheme/port, host allowlists, and resolved public IP ranges. The downloader pins its HTTPS connection to the validated IP, revalidates each redirect, and applies configured timeout/redirect/byte limits; the worker validates audio format and header-derived duration before transcription. Footage downloads use an allowlisted provider domain, resolve and pin a public IP, retain TLS hostname verification, reject redirects, and enforce a byte limit before FFprobe validation.
- Uploads are type/content validated, bounded, stored outside the source tree, and cleaned according to retention policy.
- Idempotency is durable and concurrency-safe; worker claiming is transactional.
- Provider calls have bounded timeouts/retries with retryability classification. Retries do not bypass rights, safety, or de-duplication checks.
- Logs use allowlisted structured fields and redact credentials, signed URLs, and sensitive inputs.
- S3 buckets and download links are private by default; signed link expiry is configured.
- Resource limits, request size, maximum audio duration, and concurrent worker count are configurable.

## Planned source layout

```text
src/scenepilot/
  api/                  routes, schemas, middleware, error handlers
  domain/               jobs, scenes, assets, timeline, manifest value models
  db.py models.py        SQLAlchemy engine/session setup and persistent records
  providers/            audio, TTS, scene, footage, relevance adapters and mocks
  services/             validation, planning, rights, safety, retrieval/download, rendering
  storage/              planned S3-compatible artifact adapter
  workers/              durable job execution
  config.py             validated settings
tests/
  unit/ integration/ e2e/
tools/                    batch runner and measurement utilities
testpack/                 retained fixtures, runs, outputs, and claim index
docker-compose.yml        API, worker, PostgreSQL, MinIO
Dockerfile
openapi.yaml
.env.example
README.md SOURCES.md AI_USE.md REPORT.md
```

## Acceptance and unresolved dependencies

See `REPORT.md` and `testpack/results/full-regression.txt` for the latest executable test result. The suite includes a real FFmpeg renderer integration test when FFmpeg/FFprobe are discoverable; its generated pattern/tone inputs are technical fixtures only. Docker was unavailable to verify image-provided binaries. Rights and retrieval policy tests use synthetic fixtures. No live speech provider, provider credential, per-asset watermark verification, approved production footage, measured production benchmark, reviewer result, or clean Compose run is claimed. Treat unverified rights and missing media inputs as hard blockers for publishable jobs.
