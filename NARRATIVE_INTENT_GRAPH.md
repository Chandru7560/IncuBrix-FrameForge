# Narrative Intent Graph

## Purpose and boundaries

The narrative pipeline turns script text, ordered structured scenes, or recognized speech into an explainable scene plan. It does not select or claim footage, generate actual speech in mock mode, or render a video. See [`examples/narrative_intent_graph.example.json`](./examples/narrative_intent_graph.example.json) for the readable plan shape.

## Processing flow

1. **Normalize source text.** Script text is Unicode NFC-normalized and whitespace is collapsed while the original text is retained. Structured-scene order and text are preserved; visual hints are attached to their scene.
2. **Establish timing authority.** Uploaded audio is probed and recognized through `SpeechRecognitionAdapter`; its source-audio duration and recognition timestamps govern timing. Scripts and scene lists use `TextToSpeechAdapter` output duration measured from the generated WAV. The built-in mock TTS creates a silent *timing fixture*, never purported narration.
3. **Analyze meaning.** `SemanticProvider` returns a central concept, interpretation, visual intent, entities, actions, abstract ideas, mood, sensitivity, avoid constraints, shot/composition guidance, continuity, and evidence. The deterministic provider includes contextual rules for cloud computing/weather, financial/abstract value, computer/animal mouse, financial/river bank, trust, and numerical claims. Hints are assessed against inferred meaning; a conflicting hint is retained but cannot override that meaning.
4. **Form scenes.** Sentence punctuation and recognition gaps define candidate boundaries. Multiword recognition phrases are apportioned by token length only when their internal boundaries are needed; this uncertainty is recorded as a timing source and fallback reason. Very short neighboring phrases merge, and overlong timed phrases are split.
5. **Validate the timeline.** Scene intervals are contiguous and cover the measured/recorded audio duration. The default allowed boundary tolerance is 0.05 seconds, configurable with `NARRATION_BOUNDARY_TOLERANCE_SECONDS`; plans with gaps, overlaps, or unusable timestamps fail rather than reporting false coverage.
6. **Persist explainable output.** The worker stores the original transcript, recognition warnings/provider metadata, measured duration, scene plan, selection rationale, and any deterministic-fallback reasons on the job. The job API exposes the persisted plan.

## Adapters and fallbacks

Adapter protocols live in `src/scenepilot/providers/narration.py`; orchestration is in `src/scenepilot/services/narrative_pipeline.py`. `NarrativeScenePipeline` accepts injected speech, TTS, audio-probe, and semantic adapters. Runtime configuration currently supports only deterministic mock mode; there is no production STT or TTS implementation or provider-registration mechanism. Integrators must inject a compatible adapter in the worker startup before selecting an external provider.

If semantic analysis fails or returns unusable data, deterministic contextual rules produce the plan and record the exception type and fallback method. If a configured TTS adapter fails, a silent measured timing fixture is used with a warning that it is not speech. A missing or failing speech-recognition adapter does **not** invent a transcript: the uploaded-audio job fails with an explicit error. Audio URL fetching is also not wired to this pipeline.

## Validation evidence and limits

Unit and integration tests cover ambiguous-term regressions, hint conflicts, phrase segmentation/merging/splitting, transcript preservation, generated-audio timing, provider failures, audio conditions, and contiguous coverage. FFprobe/FFmpeg are required for MP3/M4A inspection and decoding; neither executable is installed on the current workstation. No live STT/TTS provider, footage, rendered video, rights metadata, or performance result is claimed.
