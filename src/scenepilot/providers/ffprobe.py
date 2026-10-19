"""FFprobe-backed audio decoding and signal-level inspection."""

from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

from scenepilot.errors import ApiError
from scenepilot.providers.narration import AudioProbeResult


class FFprobeAudioProbe:
    def __init__(
        self,
        ffprobe_binary: str,
        ffmpeg_binary: str,
        *,
        max_audio_seconds: int,
        silence_threshold_dbfs: float,
    ) -> None:
        self.ffprobe_binary = ffprobe_binary
        self.ffmpeg_binary = ffmpeg_binary
        self.max_audio_seconds = max_audio_seconds
        self.silence_threshold_dbfs = silence_threshold_dbfs

    def probe(self, audio_path: Path) -> AudioProbeResult:
        try:
            probed = subprocess.run(
                [
                    self.ffprobe_binary,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration:stream=codec_type,codec_name,sample_rate,channels",
                    "-of",
                    "json",
                    str(audio_path),
                ],
                capture_output=True,
                check=False,
                timeout=30,
            )
        except FileNotFoundError as exc:
            raise ApiError(
                503,
                "AUDIO_PROBE_UNAVAILABLE",
                "FFprobe is required to inspect uploaded MP3 and M4A audio.",
                retryable=False,
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ApiError(
                422,
                "AUDIO_PROBE_TIMEOUT",
                "The audio could not be probed within the time limit.",
            ) from exc
        if probed.returncode != 0:
            raise ApiError(
                422,
                "CORRUPTED_AUDIO",
                "The uploaded audio could not be probed.",
                details={"diagnostic": probed.stderr.decode("utf-8", "replace")[:300]},
            )
        try:
            metadata = json.loads(probed.stdout)
            audio_streams = [
                stream for stream in metadata.get("streams", []) if stream.get("codec_type") == "audio"
            ]
            if not audio_streams:
                raise ApiError(422, "NO_AUDIO_STREAM", "The uploaded file contains no audio stream.")
            stream = audio_streams[0]
            duration = float(metadata["format"]["duration"])
            sample_rate = int(stream["sample_rate"])
            channels = int(stream["channels"])
            codec = str(stream["codec_name"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, IndexError) as exc:
            raise ApiError(
                422,
                "CORRUPTED_AUDIO",
                "The uploaded audio metadata is malformed or incomplete.",
            ) from exc
        if not math.isfinite(duration) or duration <= 0:
            raise ApiError(422, "EMPTY_AUDIO", "The audio stream has no measurable duration.")
        if duration > self.max_audio_seconds:
            raise ApiError(
                422,
                "AUDIO_TOO_LONG",
                "Audio duration exceeds the configured maximum.",
                details={
                    "duration_seconds": round(duration, 3),
                    "max_audio_seconds": self.max_audio_seconds,
                },
            )
        if sample_rate <= 0 or channels <= 0:
            raise ApiError(422, "UNSUPPORTED_AUDIO", "The audio stream has invalid channel metadata.")
        if not (
            codec == "mp3"
            or codec == "aac"
            or codec.startswith("pcm_")
        ):
            raise ApiError(
                415,
                "UNSUPPORTED_AUDIO",
                "The audio codec is not supported.",
                details={"codec": codec},
            )

        try:
            decoded = subprocess.run(
                [
                    self.ffmpeg_binary,
                    "-nostdin",
                    "-v",
                    "error",
                    "-i",
                    str(audio_path),
                    "-map",
                    "0:a:0",
                    "-f",
                    "s16le",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "pipe:1",
                ],
                capture_output=True,
                check=False,
                timeout=min(self.max_audio_seconds + 30, 660),
            )
        except FileNotFoundError as exc:
            raise ApiError(
                503,
                "AUDIO_DECODER_UNAVAILABLE",
                "FFmpeg is required to decode uploaded audio.",
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ApiError(422, "AUDIO_DECODE_TIMEOUT", "Audio decoding exceeded its time limit.") from exc
        if decoded.returncode != 0 or not decoded.stdout:
            raise ApiError(
                422,
                "CORRUPTED_AUDIO",
                "The uploaded audio could not be decoded.",
                details={"diagnostic": decoded.stderr.decode("utf-8", "replace")[:300]},
            )
        samples = [
            int.from_bytes(decoded.stdout[offset : offset + 2], "little", signed=True)
            for offset in range(0, len(decoded.stdout) - 1, 2)
        ]
        if not samples:
            raise ApiError(422, "EMPTY_AUDIO", "The decoded audio contains no samples.")
        rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
        rms_dbfs = -120.0 if rms == 0 else 20.0 * math.log10(rms / 32768.0)
        return AudioProbeResult(
            duration_seconds=duration,
            codec=codec,
            sample_rate=sample_rate,
            channels=channels,
            rms_dbfs=rms_dbfs,
            condition="silence" if rms_dbfs <= self.silence_threshold_dbfs else "unknown",
        )
