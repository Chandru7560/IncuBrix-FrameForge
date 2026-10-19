# ADR 0003: Local artifact storage before S3 integration

- **Status:** Accepted, temporary
- **Decision:** Store artifacts on the configured filesystem volume and expose allowlisted job artifact routes. Provision MinIO in Compose, but do not claim or simulate S3 uploads until that adapter is implemented and verified.
- **Reason:** Local storage is testable in the current environment; Docker and MinIO acceptance are unavailable.
- **Consequences:** Local development and tests work, but shared-volume persistence and private object delivery are not a production-ready object-storage solution. S3/MinIO integration remains a release blocker.
- **Evidence:** `src/scenepilot/worker.py`; `src/scenepilot/api/routes/jobs.py`; `docker-compose.yml`; `REPORT.md`.
