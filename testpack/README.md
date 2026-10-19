# Test pack

`cases/` contains ten request fixtures spanning meaning, dates/statistics, named entities, ambiguity, sensitive topics, portrait framing, Hindi/Tamil text, audio-duration scenarios, and multiple deliverables. These are **input fixtures**, not claimed successful outputs. `target_duration_seconds` is a coverage target, not a measured recording duration. Hindi and Tamil fixtures contain text only; they do not demonstrate speech recognition, pronunciation, or voice availability.

Current automated evidence remains in the repository's executable tests:

- `tests/unit/test_scene_planning.py` — abstract concepts and ambiguous-word interpretations.
- `tests/unit/test_input_validation.py` — empty/invalid inputs, duration, 5-minute WAV acceptance, and content mismatch.
- `tests/integration/test_narrative_pipeline.py` — silent/music-only/corrupt audio, bounded URL download and validation, provider fallbacks, timestamp/timeline integrity, and language-independent scripts.
- `tests/unit/test_visual_retrieval.py` and `tests/unit/test_footage_download.py` — provider outage/empty results, rights, unsafe candidates, duplicate selection, corrupt downloads, and provider switching.
- `tests/integration/test_jobs_api.py` — idempotency, concurrent jobs, storage failure, malformed input, UI/API routes, human ratings, and artifacts.
- `tests/unit/test_video_renderer.py` — conditional real FFmpeg output for all three ratios, captions, stream validation, cache reuse, continuity sampling, and interrupted-process handling. The real render runs when FFmpeg/FFprobe are on PATH; see `results/full-regression.txt` for the latest result.

The offline deterministic footage adapter emits synthetic metadata only. When runnable, the renderer integration test uses generated test-pattern video and a tone as a technical fixture; these are not stock footage, speech, licensed deliverables, or human-rated outputs. No production video pack is claimed until real narration and rights-verified assets are configured.

Run the batch utility against a running API:

```powershell
scenepilot-batch --input .\testpack\cases --output .\test-results --api-base-url http://127.0.0.1:8000 --aspect-ratios 16:9,9:16
```

Jobs that cannot complete with the active providers are retained as failed `job.json` records and `batch.jsonl` entries. Do not interpret fixture labels or planning-only success as a playable video.
