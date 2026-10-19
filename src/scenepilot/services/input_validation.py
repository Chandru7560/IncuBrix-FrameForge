"""Audio upload type, size, and duration validation."""

from __future__ import annotations

import io
import struct
import wave
from pathlib import Path

from scenepilot.errors import ApiError


SUPPORTED_AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a"}
SUPPORTED_AUDIO_TYPES = {
    "audio/mpeg",
    "audio/mp3",
    "audio/wav",
    "audio/x-wav",
    "audio/mp4",
    "audio/m4a",
    "application/octet-stream",
}


def validate_audio_upload(
    filename: str,
    content_type: str,
    data: bytes,
    *,
    max_upload_bytes: int,
    max_audio_seconds: int,
) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_AUDIO_SUFFIXES:
        raise ApiError(
            415,
            "UNSUPPORTED_AUDIO_FORMAT",
            "Audio must be an MP3, WAV, or M4A file.",
            details={"supported_formats": sorted(SUPPORTED_AUDIO_SUFFIXES)},
        )
    if content_type.split(";", 1)[0].strip().lower() not in SUPPORTED_AUDIO_TYPES:
        raise ApiError(415, "UNSUPPORTED_AUDIO_FORMAT", "The uploaded content type is not supported.")
    if not data:
        raise ApiError(422, "EMPTY_AUDIO", "The uploaded audio file is empty.")
    if len(data) > max_upload_bytes:
        raise ApiError(
            413,
            "UPLOAD_TOO_LARGE",
            "The uploaded audio exceeds the configured upload limit.",
            details={"max_upload_bytes": max_upload_bytes},
        )

    detected = detect_audio_format(data)
    if detected != suffix[1:]:
        raise ApiError(
            415,
            "AUDIO_CONTENT_MISMATCH",
            "The file extension does not match the uploaded audio content.",
            details={"file_extension": suffix[1:], "detected_format": detected},
        )

    duration = inspect_duration(data, detected)
    if duration is None:
        raise ApiError(
            422,
            "AUDIO_DURATION_UNVERIFIED",
            "The audio duration could not be verified from the uploaded file.",
        )
    if duration > max_audio_seconds:
        raise ApiError(
            422,
            "AUDIO_TOO_LONG",
            "Audio duration exceeds the configured maximum.",
            details={"duration_seconds": round(duration, 3), "max_audio_seconds": max_audio_seconds},
        )
    return detected


def detect_audio_format(data: bytes) -> str | None:
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "wav"
    if data.startswith(b"ID3") or (
        len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0
    ):
        return "mp3"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brands = data[8:32]
        if any(brand in brands for brand in (b"M4A ", b"M4B ", b"isom", b"mp42")):
            return "m4a"
    return None


def inspect_duration(data: bytes, audio_format: str) -> float | None:
    if audio_format == "wav":
        try:
            with wave.open(io.BytesIO(data), "rb") as source:
                if source.getframerate() <= 0:
                    return None
                return source.getnframes() / source.getframerate()
        except (wave.Error, EOFError):
            raise ApiError(422, "INVALID_AUDIO", "The WAV file is malformed.")
    if audio_format == "m4a":
        return _mp4_duration(data)
    return _mp3_duration(data)


def _mp4_duration(data: bytes) -> float | None:
    containers = {
        b"moov",
        b"trak",
        b"mdia",
        b"minf",
        b"stbl",
        b"edts",
        b"udta",
        b"ilst",
    }

    def find_movie_header(start: int, end: int) -> float | None:
        offset = start
        while offset + 8 <= end:
            size = struct.unpack_from(">I", data, offset)[0]
            atom_type = data[offset + 4 : offset + 8]
            header_size = 8
            if size == 1:
                if offset + 16 > end:
                    return None
                size = struct.unpack_from(">Q", data, offset + 8)[0]
                header_size = 16
            elif size == 0:
                size = end - offset
            if size < header_size or offset + size > end:
                return None
            payload = offset + header_size
            atom_end = offset + size
            if atom_type == b"mvhd" and size >= header_size + 20:
                version = data[payload]
                if version == 0:
                    timescale, duration = struct.unpack_from(">II", data, payload + 12)
                elif version == 1 and size >= header_size + 32:
                    timescale = struct.unpack_from(">I", data, payload + 20)[0]
                    duration = struct.unpack_from(">Q", data, payload + 24)[0]
                else:
                    return None
                return duration / timescale if timescale else None
            if atom_type in containers:
                nested_start = payload + (4 if atom_type == b"meta" else 0)
                nested_duration = find_movie_header(nested_start, atom_end)
                if nested_duration is not None:
                    return nested_duration
            offset = atom_end
        return None

    offset = 0
    while offset + 8 <= len(data):
        size = struct.unpack_from(">I", data, offset)[0]
        atom_type = data[offset + 4 : offset + 8]
        header_size = 8
        if size == 1:
            if offset + 16 > len(data):
                return None
            size = struct.unpack_from(">Q", data, offset + 8)[0]
            header_size = 16
        elif size == 0:
            size = len(data) - offset
        if size < header_size or offset + size > len(data):
            return None
        if atom_type == b"moov":
            return find_movie_header(offset + header_size, offset + size)
        offset += size
    return None


def _mp3_duration(data: bytes) -> float | None:
    offset = 0
    if data.startswith(b"ID3") and len(data) >= 10:
        tag_size_bytes = data[6:10]
        if any(byte & 0x80 for byte in tag_size_bytes):
            return None
        tag_size = (
            (tag_size_bytes[0] << 21)
            | (tag_size_bytes[1] << 14)
            | (tag_size_bytes[2] << 7)
            | tag_size_bytes[3]
        )
        offset = 10 + tag_size
    total_seconds = 0.0
    frames = 0
    while offset + 4 <= len(data):
        header = struct.unpack_from(">I", data, offset)[0]
        if (header >> 21) & 0x7FF != 0x7FF:
            offset += 1
            continue
        version = (header >> 19) & 0x3
        layer = (header >> 17) & 0x3
        bitrate_index = (header >> 12) & 0xF
        sample_rate_index = (header >> 10) & 0x3
        if version == 1 or layer != 1 or bitrate_index in (0, 15) or sample_rate_index == 3:
            offset += 1
            continue
        version_two = version == 2
        version_two_five = version == 0
        sample_rates = (44100, 48000, 32000)
        sample_rate = sample_rates[sample_rate_index]
        if version_two:
            sample_rate //= 2
        elif version_two_five:
            sample_rate //= 4
        bitrates_v1 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
        bitrates_v2 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
        bitrate = (bitrates_v2 if version_two or version_two_five else bitrates_v1)[
            bitrate_index
        ] * 1000
        padding = (header >> 9) & 1
        frame_length = (72 if version_two or version_two_five else 144) * bitrate // sample_rate
        frame_length += padding
        if frame_length < 4 or offset + frame_length > len(data):
            offset += 1
            continue
        samples = 576 if version_two or version_two_five else 1152
        total_seconds += samples / sample_rate
        frames += 1
        offset += frame_length
    return total_seconds if frames else None
