"""Speech recognition and text-to-speech provider contracts and mock adapters."""

from __future__ import annotations

import math
import struct
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from scenepilot.errors import ApiError


AudioCondition = Literal["speech", "silence", "music_only", "unsupported", "corrupt", "unknown"]


@dataclass(frozen=True)
class AudioProbeResult:
    duration_seconds: float
    codec: str
    sample_rate: int
    channels: int
    rms_dbfs: float
    condition: AudioCondition


@dataclass(frozen=True)
class TranscriptSegment:
    text: str
    start_seconds: float
    end_seconds: float
    confidence: float | None = None
    timing_source: str = "provider"


@dataclass(frozen=True)
class TranscriptionResult:
    original_text: str
    segments: tuple[TranscriptSegment, ...]
    provider: str
    model: str
    warnings: tuple[str, ...] = ()
    condition: AudioCondition = "speech"


@dataclass(frozen=True)
class SynthesisResult:
    audio_path: Path
    provider: str
    model: str
    warnings: tuple[str, ...] = ()


class AudioProbe(Protocol):
    def probe(self, audio_path: Path) -> AudioProbeResult: ...


class SpeechRecognitionAdapter(Protocol):
    def transcribe(self, audio_path: Path, probe: AudioProbeResult) -> TranscriptionResult: ...


class TextToSpeechAdapter(Protocol):
    def synthesize(self, text: str, voice: str, output_path: Path) -> SynthesisResult: ...


class ProviderUnavailableError(RuntimeError):
    """Raised when configured live audio providers are not available."""


class DeterministicMockSpeechRecognition:
    """A mock STT adapter that refuses to invent a transcript for unknown audio."""

    def transcribe(self, audio_path: Path, probe: AudioProbeResult) -> TranscriptionResult:
        raise ProviderUnavailableError(
            "Mock speech recognition does not fabricate transcripts. Configure a speech "
            "recognition adapter to process uploaded speech."
        )


class DeterministicMockTextToSpeech:
    """Create a silent timing fixture for development; this is not spoken narration."""

    def __init__(self, words_per_minute: int = 150) -> None:
        self.words_per_minute = words_per_minute

    def synthesize(self, text: str, voice: str, output_path: Path) -> SynthesisResult:
        word_count = max(1, len(text.split()))
        duration_seconds = max(1.0, word_count * 60.0 / self.words_per_minute)
        sample_rate = 8000
        frame_count = math.ceil(duration_seconds * sample_rate)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output_path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(sample_rate)
            zero_block = b"\0" * 16384
            remaining_frames = frame_count
            while remaining_frames:
                block_frames = min(remaining_frames, len(zero_block) // 2)
                audio.writeframesraw(zero_block[: block_frames * 2])
                remaining_frames -= block_frames
        del voice
        return SynthesisResult(
            audio_path=output_path,
            provider="mock",
            model="silent-timing-fixture",
            warnings=(
                "MOCK_TTS_SILENT_FIXTURE: duration is measured from a generated silent WAV; "
                "it is not speech and must not be used as narration.",
            ),
        )


def probe_pcm_wav(audio_path: Path, *, silence_threshold_dbfs: float) -> AudioProbeResult:
    try:
        with wave.open(str(audio_path), "rb") as audio:
            frame_count = audio.getnframes()
            sample_rate = audio.getframerate()
            channels = audio.getnchannels()
            sample_width = audio.getsampwidth()
            frames = audio.readframes(frame_count)
    except (OSError, EOFError, wave.Error) as exc:
        raise ApiError(
            422,
            "CORRUPTED_AUDIO",
            "The WAV file could not be decoded.",
            details={"reason": type(exc).__name__},
        ) from exc
    if frame_count == 0 or sample_rate <= 0 or not frames:
        raise ApiError(422, "EMPTY_AUDIO", "The audio stream contains no sample frames.")
    if sample_width not in {1, 2, 3, 4} or channels < 1:
        raise ApiError(422, "UNSUPPORTED_AUDIO", "The WAV encoding is not supported.")
    duration = frame_count / sample_rate
    if sample_width == 1:
        values = [sample - 128 for sample in frames]
        peak = 128.0
    else:
        values = []
        for offset in range(0, len(frames) - sample_width + 1, sample_width):
            sample = int.from_bytes(
                frames[offset : offset + sample_width],
                byteorder="little",
                signed=True,
            )
            values.append(sample)
        peak = float(2 ** (sample_width * 8 - 1))
    if not values:
        raise ApiError(422, "CORRUPTED_AUDIO", "The WAV samples could not be decoded.")
    rms = math.sqrt(sum(value * value for value in values) / len(values))
    rms_dbfs = -120.0 if rms == 0 else 20.0 * math.log10(rms / peak)
    condition: AudioCondition = (
        "silence" if rms_dbfs <= silence_threshold_dbfs else "unknown"
    )
    return AudioProbeResult(
        duration_seconds=duration,
        codec="pcm",
        sample_rate=sample_rate,
        channels=channels,
        rms_dbfs=rms_dbfs,
        condition=condition,
    )


def estimate_word_segments(
    text: str,
    duration_seconds: float,
    *,
    source: str,
) -> tuple[TranscriptSegment, ...]:
    """Create word timings for known script text when mock speech has no word alignment."""
    words = text.split()
    if not words:
        return ()
    weights = [max(1, len(word.strip(".,!?;:"))) for word in words]
    total_weight = sum(weights)
    cursor = 0.0
    segments = []
    for index, (word, weight) in enumerate(zip(words, weights)):
        end = duration_seconds if index == len(words) - 1 else cursor + duration_seconds * weight / total_weight
        segments.append(TranscriptSegment(word, cursor, end))
        cursor = end
    return tuple(segments)
