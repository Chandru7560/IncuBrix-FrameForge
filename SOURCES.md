# Sources, models, datasets, and dependency licences

This register describes what the repository can use and what is actually enabled. No provider credentials are committed. A provider appearing in the adapter list is not evidence that it was called or that a particular asset's rights were verified.

## Media providers and content

| Source | Purpose | Terms/licence reference | Status in this repository |
|---|---|---|---|
| Pexels API / Pexels video library | Optional stock-video search via `PexelsVideoAdapter`. | [Pexels API](https://www.pexels.com/api/documentation/) and [Pexels License](https://www.pexels.com/license/). Provider content must be used under the then-current Pexels terms and restrictions. | Adapter implemented; no API key or live call was available during implementation. License-page retrieval was blocked in this environment. Pexels does not provide per-asset licence evidence in the fields consumed here, so a provider-wide operator attestation is required, and the current API metadata does not verify watermark absence; candidates remain ineligible until an adapter/verifier supplies that evidence. |
| Pixabay API / Pixabay video library | Independent optional stock-video search via `PixabayVideoAdapter`. | [Pixabay API documentation](https://pixabay.com/api/docs/) and [Pixabay Content License / terms](https://pixabay.com/service/terms/). Pixabay's API documentation requests source attribution when displaying search results and disallows systematic mass downloads; API results are documented as cached for 24 hours. | Adapter implemented; no API key or live call was available. The API response does not establish asset-level usage rights or watermark status. Without explicit operator approval and a verified watermark assessment, candidates are unverified/ineligible. |
| Deterministic local footage metadata fixture | Offline adapter tests and local development. | No media licence is granted; the adapter returns synthetic metadata only, not footage. | Always non-publishable, marked `test_fixture`, no original/download URL, rights unverified. It cannot be selected or rendered. |
| Generative media | No generative-media provider/model is integrated. | Not applicable. | Generative fallback is disabled. No generated image/video is represented as genuine footage. |
| Background music | Renderer accepts an injected `LicensedBackgroundMusic` value only after rights validation. | Must be supplied by an integration with its own verified provider terms, licence evidence, and attribution record. | No music provider, model, or track is bundled/configured. Output uses narration only unless verified licensed music is injected. |

`PEXELS_RIGHTS_POLICY_APPROVED=true` and `PIXABAY_RIGHTS_POLICY_APPROVED=true` are explicit operator attestations that the corresponding current terms were reviewed for the intended use; each also requires its API key. This flag alone is not per-asset proof, and it does not establish that a download is unwatermarked or content-safe. Current provider APIs return `watermarked=null`, which the selection policy rejects. Do not enable publishable use by treating a successful API response, a search result, or these attestations as a substitute for missing asset-specific evidence. Supply an adapter/verifier with evidence for each required rights and watermark check.

No provider search, model, media dataset, narration provider, or production asset was used to produce this implementation or its tests.

## Optional relevance-evaluation endpoint

The optional `openai_compatible` evaluator is an HTTP adapter, not a bundled or default-enabled model. The operator supplies the endpoint, model identifier/version, API key, and terms assessment through environment configuration. It samples frames from the selected downloaded video and evaluates them with that scene's narration/intent; it does not evaluate only the original search query. The adapter requests a 0–100 score and normalizes it by dividing by 100. It records the configured provider/model, returned version, rationale, and sampled timestamps. No model weights, dataset, endpoint, credentials, actual evaluation results, or commercial-use claim are included in this repository.

When enabled, sampled video frames and narration/intent are sent to the configured external service. The operator is responsible for confirming privacy, retention, and commercial terms before enabling the integration. A disabled, failing, or inaccessible evaluator produces a null score and an explicit unavailable status; it does not substitute a heuristic score.

## Runtime and development dependencies

| Dependency | Purpose | Licence (upstream project) | Distribution note |
|---|---|---|---|
| Python 3.12 | Runtime | PSF License | System runtime; not bundled in application source. |
| FastAPI | API framework/OpenAPI | MIT | Declared in `pyproject.toml`. |
| Starlette | ASGI framework | BSD-3-Clause | Declared transitively/explicitly in `pyproject.toml`. |
| Pydantic | Request/response validation | MIT | Declared in `pyproject.toml`. |
| HTTPX | Provider/API and media-download HTTP client | BSD-3-Clause | Declared in `pyproject.toml`. |
| SQLAlchemy | Persistent job records | MIT | Declared in `pyproject.toml`. |
| psycopg2-binary | PostgreSQL driver | LGPL-3.0-with-exception | Declared in `pyproject.toml`; inspect wheel notices for redistribution. |
| python-multipart | Multipart upload parsing | Apache-2.0 | Declared in `pyproject.toml`. |
| Uvicorn | ASGI server | BSD-3-Clause | Declared in `pyproject.toml`. |
| pytest | Test runner | MIT | Test extra in `pyproject.toml`. |
| PyYAML | OpenAPI contract test | MIT | Test extra in `pyproject.toml`. |
| FFmpeg / FFprobe | Media decode, crop/scale, normalization, subtitle composition, H.264/AAC encoding, and validation | FFmpeg is LGPL-2.1-or-later for LGPL-only builds; GPL components change the obligations to GPL. See [FFmpeg legal information](https://ffmpeg.org/legal.html), [FFmpeg license](https://ffmpeg.org/license.html), and [libx264](https://code.videolan.org/videolan/x264). | The application does not bundle FFmpeg binaries. Docker installs the distribution's FFmpeg package. The renderer requires `libx264`, `subtitles`, `minterpolate`, `blackdetect`, and `freezedetect`; inspect `ffmpeg -buildconf` for the exact deployed build and satisfy its source/license obligations. H.264 patent licensing is separate from software licensing. Commercial deployment requires review of codec patent obligations and the exact FFmpeg build. |

The system image's exact transitive OS packages and FFmpeg build flags depend on the selected base-image repository at build time; retain the generated package SBOM and `ffmpeg -version`/`ffmpeg -buildconf` with release evidence. License names above identify upstream project licences and are not legal advice.
