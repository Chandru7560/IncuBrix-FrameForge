from __future__ import annotations

import io
import socket
import struct
import wave

import pytest

from scenepilot.api.schemas import JobCreateRequest
from scenepilot.errors import ApiError
from scenepilot.services.input_validation import validate_audio_upload
from scenepilot.services.url_safety import fetch_public_audio, validate_public_https_url


def wav_file(duration_seconds: int = 1) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 8000 * duration_seconds)
    return output.getvalue()


def test_script_request_accepts_valid_ratios_and_captions():
    request = JobCreateRequest.model_validate(
        {"script": "A clear script.", "aspect_ratios": ["16:9", "9:16"]}
    )
    assert request.script == "A clear script."
    assert request.aspect_ratios == ["16:9", "9:16"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"script": " "},
        {"script": "text", "audio_url": "https://example.com/audio.mp3"},
        {"scenes": []},
        {"script": "text", "aspect_ratios": ["4:3"]},
    ],
)
def test_invalid_job_request_inputs_are_rejected(payload):
    with pytest.raises(ValueError):
        JobCreateRequest.model_validate(payload)


def test_scene_request_requires_text_and_accepts_visual_hint():
    request = JobCreateRequest.model_validate(
        {"scenes": [{"text": "A calm forest", "visual_hint": "wide green landscape"}]}
    )
    assert request.scenes[0].visual_hint == "wide green landscape"


def test_wav_upload_content_is_validated():
    assert (
        validate_audio_upload(
            "voice.wav",
            "audio/wav",
            wav_file(),
            max_upload_bytes=1024 * 1024,
            max_audio_seconds=10,
        )
        == "wav"
    )


def test_upload_rejects_extension_content_mismatch():
    with pytest.raises(ApiError) as error:
        validate_audio_upload(
            "voice.mp3",
            "audio/mpeg",
            wav_file(),
            max_upload_bytes=1024 * 1024,
            max_audio_seconds=10,
        )
    assert error.value.code == "AUDIO_CONTENT_MISMATCH"


def test_upload_enforces_size_and_duration():
    with pytest.raises(ApiError) as size_error:
        validate_audio_upload(
            "voice.wav",
            "audio/wav",
            wav_file(),
            max_upload_bytes=8,
            max_audio_seconds=10,
        )
    assert size_error.value.status_code == 413

    with pytest.raises(ApiError) as duration_error:
        validate_audio_upload(
            "voice.wav",
            "audio/wav",
            wav_file(2),
            max_upload_bytes=1024 * 1024,
            max_audio_seconds=1,
        )
    assert duration_error.value.code == "AUDIO_TOO_LONG"


def test_five_minute_wav_is_accepted_under_ten_minute_limit():
    data = wav_file(300)
    assert (
        validate_audio_upload(
            "five-minute.wav",
            "audio/wav",
            data,
            max_upload_bytes=10 * 1024 * 1024,
            max_audio_seconds=600,
        )
        == "wav"
    )


def test_m4a_duration_is_read_from_nested_movie_header():
    movie_header = (
        b"\x00\x00\x00\x00"
        + struct.pack(">IIII", 0, 0, 1000, 601000)
        + b"\x00" * 20
    )
    mvhd = struct.pack(">I4s", 8 + len(movie_header), b"mvhd") + movie_header
    moov = struct.pack(">I4s", 8 + len(mvhd), b"moov") + mvhd
    file_type = struct.pack(">I4s", 20, b"ftyp") + b"M4A " + b"\x00\x00\x00\x00" + b"M4A "
    with pytest.raises(ApiError) as error:
        validate_audio_upload(
            "voice.m4a",
            "audio/mp4",
            file_type + moov,
            max_upload_bytes=1024 * 1024,
            max_audio_seconds=600,
        )
    assert error.value.code == "AUDIO_TOO_LONG"


def test_mp3_without_verifiable_frames_is_rejected():
    with pytest.raises(ApiError) as error:
        validate_audio_upload(
            "voice.mp3",
            "audio/mpeg",
            b"ID3\x04\x00\x00\x00\x00\x00\x00",
            max_upload_bytes=1024,
            max_audio_seconds=600,
        )
    assert error.value.code == "AUDIO_DURATION_UNVERIFIED"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/audio.mp3",
        "https://127.0.0.1/audio.mp3",
        "https://user:password@example.com/audio.mp3",
        "https://example.com:8443/audio.mp3",
        "https://example.com/audio.mp3#fragment",
    ],
)
def test_unsafe_audio_urls_are_rejected(url):
    with pytest.raises(ApiError) as error:
        validate_public_https_url(url, allow_hosts=frozenset())
    assert error.value.code == "UNSAFE_AUDIO_URL"


def test_audio_url_allowlist_is_enforced():
    with pytest.raises(ApiError) as error:
        validate_public_https_url(
            "https://other.example/audio.mp3",
            allow_hosts=frozenset({"trusted.example"}),
        )
    assert error.value.code == "UNSAFE_AUDIO_URL"


def test_audio_url_rejects_dns_answers_mixing_public_and_private_addresses():
    def resolver(host, port, *, type):
        return [
            (socket.AF_INET, type, socket.IPPROTO_TCP, "", ("93.184.216.34", port)),
            (socket.AF_INET, type, socket.IPPROTO_TCP, "", ("127.0.0.1", port)),
        ]

    with pytest.raises(ApiError) as error:
        validate_public_https_url(
            "https://example.com/audio.wav",
            allow_hosts=frozenset(),
            resolver=resolver,
        )
    assert error.value.code == "UNSAFE_AUDIO_URL"


def test_audio_url_fetch_validates_each_redirect_and_enforces_size(
    tmp_path, monkeypatch
):
    import scenepilot.services.url_safety as url_safety

    class Response:
        def __init__(self, status, headers, body=b""):
            self.status = status
            self.headers = headers
            self.body = body

        def getheader(self, name):
            return self.headers.get(name.lower())

        def read(self, size):
            chunk, self.body = self.body[:size], self.body[size:]
            return chunk

    responses = [
        Response(302, {"location": "https://cdn.example.com/voice.wav"}),
        Response(200, {}, b"validaudio"),
    ]
    checked_urls = []
    connections = []

    class Connection:
        def __init__(self, hostname, pinned_ip, *, timeout):
            assert hostname in {"example.com", "cdn.example.com"}
            assert pinned_ip == "93.184.216.34"
            assert timeout == 3
            self.response = responses.pop(0)
            connections.append(self)

        def request(self, method, request_path, headers):
            assert method == "GET"
            assert request_path == "/voice.wav"
            assert headers["Accept"] == "audio/*"
            assert headers["Connection"] == "close"

        def getresponse(self):
            return self.response

        def close(self):
            pass

    def validate_and_resolve(url, *, allow_hosts):
        checked_urls.append(url)
        parsed = url_safety.urlsplit(url)
        return parsed, parsed.hostname, ["93.184.216.34"]

    monkeypatch.setattr(
        url_safety,
        "_validate_and_resolve",
        validate_and_resolve,
    )
    monkeypatch.setattr(url_safety, "_PinnedHTTPSConnection", Connection)
    destination = tmp_path / "remote.download"

    size = fetch_public_audio(
        "https://example.com/voice.wav",
        destination=destination,
        max_bytes=16,
        timeout_seconds=3,
        max_redirects=1,
        allow_hosts=frozenset(),
    )

    assert size == len(b"validaudio")
    assert destination.read_bytes() == b"validaudio"
    assert checked_urls == [
        "https://example.com/voice.wav",
        "https://cdn.example.com/voice.wav",
    ]
    assert len(connections) == 2


def test_audio_url_fetch_removes_partial_download_when_size_limit_is_exceeded(
    tmp_path, monkeypatch
):
    import scenepilot.services.url_safety as url_safety

    class Response:
        status = 200
        headers = {}
        body = b"too large"

        def getheader(self, name):
            return self.headers.get(name.lower())

        def read(self, size):
            chunk, self.body = self.body[:size], self.body[size:]
            return chunk

    class Connection:
        def __init__(self, hostname, pinned_ip, *, timeout):
            self.response = Response()

        def request(self, *_args, **_kwargs):
            pass

        def getresponse(self):
            return self.response

        def close(self):
            pass

    monkeypatch.setattr(
        url_safety,
        "_validate_and_resolve",
        lambda url, *, allow_hosts: (url_safety.urlsplit(url), "example.com", ["93.184.216.34"]),
    )
    monkeypatch.setattr(url_safety, "_PinnedHTTPSConnection", Connection)
    destination = tmp_path / "partial.download"

    with pytest.raises(ApiError) as error:
        fetch_public_audio(
            "https://example.com/voice.wav",
            destination=destination,
            max_bytes=4,
            timeout_seconds=3,
            max_redirects=0,
            allow_hosts=frozenset(),
        )

    assert error.value.code == "UPLOAD_TOO_LARGE"
    assert not destination.exists()
