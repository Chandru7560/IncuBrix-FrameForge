# ADR 0002: Fail closed on narration and media rights

- **Status:** Accepted
- **Decision:** Never fabricate transcripts, spoken narration, footage, commercial rights, attribution, or watermark clearance. Silent mock TTS is a timing fixture only. A production render requires real narration and eligible footage with rights evidence.
- **Reason:** A successful-looking video with invented speech or unverified licensing would misrepresent both product output and assessment evidence.
- **Consequences:** Offline mode supports planning and deterministic tests but may correctly fail video jobs. Real speech/media integrations must provide provenance and preserve explicit failure details.
- **Evidence:** `src/scenepilot/providers/narration.py`; `src/scenepilot/services/visual_retrieval.py`; `src/scenepilot/services/video_renderer.py`; `tests/integration/test_narrative_pipeline.py`.
