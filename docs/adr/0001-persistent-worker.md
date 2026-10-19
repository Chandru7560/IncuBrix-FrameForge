# ADR 0001: Persisted polling worker

- **Status:** Accepted
- **Decision:** Store job and idempotency state in SQLAlchemy-backed SQLite for local development and PostgreSQL in Compose. Let a separate polling worker claim queued jobs instead of processing long work in an HTTP request.
- **Reason:** This keeps submissions responsive, makes state inspectable, and avoids requiring a broker for the deterministic local workflow.
- **Consequences:** Workers must claim jobs transactionally and mark every processing/output-validation failure terminal. Polling is simpler but does not provide broker-level scheduling or verified multi-worker throughput.
- **Evidence:** `src/scenepilot/worker.py`; `tests/integration/test_jobs_api.py`.
