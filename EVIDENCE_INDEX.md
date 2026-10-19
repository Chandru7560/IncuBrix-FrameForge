# Evidence index

This index points to repository evidence and states what it does and does not prove. It does not claim an external deployment, live provider call, commercial media licence, or reviewer outcome.

| Claim | Evidence | Scope / caveat |
|---|---|---|
| API route contract and input validation | `openapi.yaml`; `tests/integration/test_jobs_api.py`; `tests/unit/test_input_validation.py`; `tests/integration/test_narrative_pipeline.py` | URL downloads are byte-, timeout-, redirect-, audio-format-, and duration-validated before transcription. No live STT provider is configured, so URL ingestion is not proof of a completed video. |
| Persistent jobs, progress, and idempotent concurrency | `tests/integration/test_jobs_api.py`; `src/scenepilot/api/routes/jobs.py`; `src/scenepilot/worker.py` | Local database/test worker evidence; no deployed load run. |
| Contextual scene planning/timeline coverage | `tests/unit/test_scene_planning.py`; `tests/integration/test_narrative_pipeline.py`; `NARRATIVE_INTENT_GRAPH.md` | Deterministic planning and injected adapter tests; no provider-generated transcript. |
| Rights and provider failover | `tests/unit/test_visual_retrieval.py`; `tests/unit/test_footage_download.py`; `SOURCES.md` | Synthetic provider fixtures; no real asset licence/watermark clearance. |
| Real encoded media pipeline | `tests/unit/test_video_renderer.py`; `testpack/results/` | FFmpeg test-pattern/tone sources. Technical codec/render validation only, not usable narration or commercial footage. |
| Frontend workspace and API-backed controls | `src/scenepilot/studio/index.html`; `src/scenepilot/studio/studio.js`; `tests/integration/test_jobs_api.py` | Static serving and endpoint integration tests; local browser verification is environment-dependent. |
| Human ratings and independent VLM evaluator | `src/scenepilot/api/routes/jobs.py`; `src/scenepilot/services/relevance_evaluation.py`; `src/scenepilot/evaluation_report.py`; corresponding tests | Endpoints/report/evaluator behavior tested; no credentials, actual model output, reviewer sample, or agreement value. |
| Batch runner | `src/scenepilot/batch_runner.py`; `tests/unit/test_batch_runner.py`; `testpack/README.md` | Unit-tested file/folder handling; no run against live licensed providers. |
| Ten hard-case request fixtures | `testpack/cases/`; `testpack/README.md` | Inputs only. Accent/duration metadata is descriptive; no speech/output is implied. |
| Full regression test command and actual outcome | `testpack/results/full-regression.txt`; `testpack/results/test-index.json` | Captured output from the latest full run, including whether the conditional FFmpeg render executed. |
| Actual successful planning-only and failed render job outcomes | `testpack/results/planning-only-request.json`; `testpack/results/planning-only-job.json`; `testpack/results/render-failed-job.json` | Persisted local API responses. Planning success is not video success; render failure correctly reports missing real narration. |
| Local browser/API smoke and responsive check | `testpack/results/browser-smoke.json` | Latest run records a real UI submission and safe failed job when mock TTS cannot produce narration; historical planning-only smoke is retained. No production video output. |
| Deployment | `Dockerfile`; `docker-compose.yml`; `DEPLOYMENT.md` | Compose is present but Docker was unavailable and startup was not verified. |

Output files (`video_*.mp4`, `captions.srt`, `timeline.json`, `manifest.json`) exist only when an actual successful render creates them. No production video pack is included or claimed in this repository.
