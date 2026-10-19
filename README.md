# FrameForge Studio / ScenePilot AI

FrameForge Studio is a responsive, dark creative-production workspace served by the ScenePilot FastAPI application. It submits scripts, structured scenes, or audio uploads/URLs; tracks persistent background jobs; displays the scene plan and Rights Passport metadata; accepts human scene ratings; and provides artifact downloads and aspect-ratio comparisons when the API has real rendered results.

**Important limitations:** no live STT/TTS provider is wired into worker startup. By default, `RENDER_ENABLED=false` keeps the local app useful for scene planning without treating the silent mock TTS fixture as real narration; no video artifacts are created. Rendering must be explicitly enabled only after real narration and verified footage are configured, otherwise the job fails closed. Mock speech recognition refuses to fabricate a transcript. Public audio URLs are fetched with size, redirect, format, and duration checks, but transcription fails explicitly until a real STT adapter is configured. Pexels/Pixabay adapters require credentials, and current metadata does not verify per-asset watermark clearance. Local footage metadata is synthetic and never publishable. Scene-segment caching exists, but there is no scene-replacement API or measured partial-versus-full replacement workflow. MinIO is included in Compose but artifact upload to S3 is not implemented; files are stored under the configured output directory. Docker Compose has not been verified in this environment.

## Run locally

Requires Python 3.12.

```powershell
python -m pip install -e ".[test]"
python -m uvicorn scenepilot.main:app --reload
```

Open `http://127.0.0.1:8000/` for FrameForge Studio. The local default uses SQLite at `./data/scenepilot.db` and stores inputs/outputs in `./data/inputs` and `./data/outputs`. The deterministic mock worker runs in the API process; no provider credentials are required. Local defaults create clearly disclosed scene plans and do not provide playable videos. Set `RENDER_ENABLED=true` only when real narration, eligible rights-verified footage, and render validation are configured.

Run the full offline regression suite:

```powershell
python -m pytest tests\unit tests\integration -o addopts= -q
```

## Run with Docker Compose

Copy `.env.example` to `.env`, then start the stack:

```powershell
Copy-Item .env.example .env
docker compose up --build
```

Compose runs the API, a separate worker, PostgreSQL, and MinIO. The example database and MinIO passwords are for trusted local development only; replace them before any broader exposure. Job records use PostgreSQL; API and worker share the configured input, output, and render-cache volumes. MinIO runs as a local service but artifact uploads are not yet wired.

## API

All business routes use `/api/v1/`; `/health` and `/ready` are operational probes.

### `POST /api/v1/broll/jobs`

Every POST requires a printable ASCII `Idempotency-Key` of 1–128 characters. Reusing the key with the same normalized request returns the original `job_id`; using it with a different request returns `409 IDEMPOTENCY_KEY_REUSED`.

For JSON requests, supply exactly one of:

```json
{"script": "A short narration.", "aspect_ratios": ["16:9", "9:16"]}
```

```json
{"scenes": [{"text": "A cyclist rides through a city.", "visual_hint": "Wide urban shot"}]}
```

```json
{"audio_url": "https://public.example/voiceover.mp3"}
```

Audio files are submitted as `multipart/form-data` in the `file` field. MP3, WAV, and M4A are accepted up to the configured byte and duration limits when duration can be inspected from the file headers. Audio URLs must use HTTPS on port 443, have no embedded credentials or fragments, and resolve only to public IP addresses. `ALLOWED_AUDIO_HOSTS`, when configured, restricts URL hosts further. The worker pins its HTTPS connection to a validated public IP and revalidates redirects; it enforces byte, timeout, format, and duration limits before passing downloaded audio through the same recognition path as an upload. Since the built-in mock recognizer intentionally does not fabricate transcripts, a valid URL job still fails with `SPEECH_RECOGNITION_UNAVAILABLE` unless an injected speech adapter is configured.

Creation returns `202 Accepted` with `job_id`, status, progress, and `status_url`. Poll `GET /api/v1/jobs/{job_id}` for status, warnings, and result links. States are `queued`, `running`, `succeeded`, and `failed`. Successful renders expose `captions.srt`, `timeline.json`, `manifest.json`, and one `video_{ratio}.mp4` per requested ratio through `GET /api/v1/jobs/{job_id}/artifacts/{artifact_name}`. Only persisted artifacts for succeeded jobs are served.

### Errors and diagnostics

Errors use this shape:

```json
{
  "error": {
    "code": "UNSUPPORTED_AUDIO_FORMAT",
    "message": "Audio must be an MP3, WAV, or M4A file.",
    "retryable": false,
    "details": {}
  }
}
```

Codes currently used include `AUDIO_CONTENT_MISMATCH`, `AUDIO_DURATION_UNVERIFIED`, `AUDIO_TOO_LONG`, `AUDIO_URL_FETCH_FAILED`, `AUDIO_URL_REDIRECT_REJECTED`, `DEPENDENCY_NOT_READY`, `EMPTY_AUDIO`, `HTTP_ERROR`, `IDEMPOTENCY_CONFLICT`, `IDEMPOTENCY_KEY_REUSED`, `INPUT_STORAGE_FAILED`, `INVALID_AUDIO`, `INVALID_FILENAME`, `INVALID_IDEMPOTENCY_KEY`, `INVALID_JSON`, `INVALID_MULTIPART`, `JOB_NOT_FOUND`, `JOB_PROCESSING_FAILED`, `METHOD_NOT_ALLOWED`, `MISSING_AUDIO_FILE`, `MISSING_IDEMPOTENCY_KEY`, `MULTIPLE_INPUTS`, `NOT_FOUND`, `REQUEST_TOO_LARGE`, `TOO_MANY_SCENES`, `UNSAFE_AUDIO_URL`, `UNKNOWN_MULTIPART_FIELD`, `UNSUPPORTED_AUDIO_FORMAT`, `UNSUPPORTED_CONTENT_TYPE`, `UPLOAD_TOO_LARGE`, `URL_VALIDATION_TIMEOUT`, and `VALIDATION_ERROR`.

JSON logs include an ISO timestamp, severity/level, `job_id`, and `X-Correlation-Id`. A valid incoming correlation ID is returned in that response header; otherwise one is generated.

## Configuration

`.env.example` lists runtime and Compose settings, including worker mode, upload/audio limits, URL timeouts, database paths, footage source order and score weights, rights approvals, download limits, FFmpeg binaries, render timeouts, and cache/output directories. Settings are validated during startup. Provider credentials are runtime-only and must not be logged or committed.

### Footage selection and rights

Pexels and Pixabay are independent optional search adapters; source order, minimum relevance, aspect tolerance, and score weights are configurable. Eligible candidates are screened for relevance, aspect/duration, duplicate use, avoid terms, safety, commercial-rights evidence, and explicit clear-watermark status. Source responses are retained as decision provenance; a successful HTTP search is never considered rights proof. Candidate failure/outage falls through to the next provider. Read [`SOURCES.md`](./SOURCES.md) before enabling any provider; the current providers do not independently prove asset-level rights or watermark status.

### Rendering and validation

The FFmpeg renderer builds content-hashed per-scene segments, assembles each requested ratio at 30 fps with H.264/AAC, produces SRT/timeline/manifest files, and validates stream metadata, duration, decoding, coverage, duplicates, black frames, and long freezes. Short footage can be extended only with bounded motion-compensated slow motion; it is never looped or frozen to fill narration. Background music is accepted only through a rights-validated integration. If FFmpeg/FFprobe, real narration, a cleared asset, or output validation is unavailable, the job fails instead of returning a simulated video. Scene replacement and S3 uploads remain unimplemented.

## OpenAPI and tests

The OpenAPI 3.1 contract is in [`openapi.yaml`](./openapi.yaml) and is compared against FastAPI's generated schema by an integration test.

```powershell
python -m pytest tests\unit tests\integration -o addopts= -q
```

The test suite requires no paid services or credentials. FFmpeg/FFprobe enable the real-render test; it skips when either executable is unavailable. The latest completed full-suite result is recorded in [`REPORT.md`](./REPORT.md); policy tests use deterministic fixtures and do not establish real-provider availability or commercial permission. Docker/Compose behavior has not been validated.

## Narrative scene planning

The semantic rules, phrase segmentation, timeline validation, adapter boundaries, fallback behavior, and current limits are described in [`NARRATIVE_INTENT_GRAPH.md`](./NARRATIVE_INTENT_GRAPH.md). A readable example response is in [`examples/narrative_intent_graph.example.json`](./examples/narrative_intent_graph.example.json). Retrieval policy and rights disclosures are in [`SOURCES.md`](./SOURCES.md); targeted tests are in `tests/unit/test_visual_retrieval.py`, `tests/unit/test_footage_download.py`, and `tests/unit/test_video_renderer.py`.

## FrameForge workspace and evaluation tools

The workspace loads only local static assets; it does not include fabricated recent projects or mock success metrics. Job submission, polling, ratings, and result downloads call the API. Scene replacement is not available and is labelled accordingly. Existing retrieval relevance values are heuristic ranks, not calibrated confidence or independent model scores.

- [`testpack/README.md`](./testpack/README.md) describes ten request fixtures and evidence boundaries.
- `scenepilot-batch --input <file-or-folder> --output <directory>` submits supported `.mp3`, `.wav`, `.m4a`, `.txt`, and `.json` inputs, polls each job, and retains failed `job.json` records and a `batch.jsonl` run log.
- `scenepilot-evaluate-ratings --database-url ...` reports human/model rating agreement only when usable persisted data is present. Human ratings are normalized from 1–5 via `(rating - 1) / 4`; evaluator scores use the configured 0–100 model scale divided by 100. No reviewer results are currently claimed.
- The optional independent evaluator is disabled by default. Enabling it sends sampled frames from the selected asset plus scene narration/intent to the configured OpenAI-compatible endpoint; review privacy and provider terms before use.

See [`ARCHITECTURE.md`](./ARCHITECTURE.md), [`DEVELOPMENT_PLAN.md`](./DEVELOPMENT_PLAN.md), [`REQUIREMENTS_TRACEABILITY.md`](./REQUIREMENTS_TRACEABILITY.md), [`RISKS.md`](./RISKS.md), [`SOURCES.md`](./SOURCES.md), [`REPORT.md`](./REPORT.md), [`DEPLOYMENT.md`](./DEPLOYMENT.md), and [`EVIDENCE_INDEX.md`](./EVIDENCE_INDEX.md) for implementation status and reproducible evidence.
