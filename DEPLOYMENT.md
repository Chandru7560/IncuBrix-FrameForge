# Deployment and smoke-test guide

## Local development

Prerequisites: Python 3.12, FFmpeg/FFprobe for real-render tests, and no provider key for offline tests.

The local default uses `RENDER_ENABLED=false`: script/scene jobs create disclosed plans only. Set `RENDER_ENABLED=true` only after a real narration adapter, eligible rights-verified footage, and output validation are configured; otherwise jobs must fail rather than return a simulated video.

```powershell
python -m pip install -e ".[test]"
python -m uvicorn scenepilot.main:app --host 127.0.0.1 --port 8000
```

In a second terminal:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/ready
Invoke-WebRequest http://127.0.0.1:8000/ | Select-Object -ExpandProperty StatusCode
python -m pytest tests\unit tests\integration -o addopts= -q
```

For script submission, use a unique idempotency key:

```powershell
$headers = @{ "Idempotency-Key" = [guid]::NewGuid().ToString() }
$body = @{ script = "A brief sample narration."; aspect_ratios = @("16:9") } | ConvertTo-Json
$job = Invoke-RestMethod -Uri http://127.0.0.1:8000/api/v1/broll/jobs -Method Post -Headers $headers -ContentType "application/json" -Body $body
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/jobs/$($job.job_id)"
```

A mock script job can produce a successful **scene plan only**. It does not demonstrate narration or a playable video. The expected render-enabled behavior without real narration and eligible footage is an explicit failed job, not simulated success.

## Docker Compose

```powershell
Copy-Item .env.example .env
docker compose up --build
```

Compose configures FastAPI, a worker, PostgreSQL, and MinIO. Replace placeholder local credentials before any wider exposure. MinIO currently provides readiness only; generated outputs still use the shared filesystem volume, so S3 persistence is not operational. Clean-environment Compose startup and deployment health have **not** been verified in the current environment because Docker is unavailable.

## Optional provider configuration

Provider keys belong in an untracked `.env` file or secret manager, never source, logs, screenshots, test fixtures, or committed evidence. Read [`SOURCES.md`](./SOURCES.md) and provider terms before configuration. Speech providers are not currently wired into worker startup. Pexels/Pixabay results remain ineligible for publishing unless an integration supplies asset-specific licence/watermark evidence. The optional evaluator sends sampled selected-video frames plus scene narration/intent to the configured external endpoint; confirm privacy, retention, and commercial terms first.

## Reproducible smoke checklist

1. Start API and worker with the intended environment.
2. Check `/health`, `/ready`, and `/`; verify static CSS/JS load locally.
3. Submit an idempotent script/scenes planning request; poll the returned job ID to a terminal state.
4. Retry the exact request with the same key and verify the same job ID; change the payload with the same key and verify a structured conflict.
5. Submit an invalid request and a silent WAV; verify structured errors and no fabricated transcript.
6. Run the complete offline test command above.
7. For production readiness only: submit real speech and cleared source assets; download and inspect MP4, SRT, timeline, manifest, rights details, and output checks.

Do not treat steps 1–6 as evidence of a production video. Record actual command output, timestamps, provider configuration (without secrets), and artifact hashes in the evidence pack.
