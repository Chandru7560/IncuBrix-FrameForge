"""Bounded, provider-host-restricted downloads with FFprobe validation."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import socket
import ssl
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from scenepilot.providers.footage import FootageCandidate


class FootageAssetDownloadError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class DownloadedFootage:
    path: Path
    size_bytes: int
    sha256: str
    width: int
    height: int


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, pinned_ip: str, *, timeout: float) -> None:
        super().__init__(hostname, port=443, timeout=timeout, context=ssl.create_default_context())
        self.pinned_ip = pinned_ip

    def connect(self) -> None:
        sock = socket.create_connection(
            (self.pinned_ip, self.port),
            self.timeout,
            self.source_address,
        )
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def download_and_validate_footage(
    candidate: FootageCandidate,
    *,
    destination_directory: Path,
    max_bytes: int,
    timeout_seconds: float,
    ffprobe_binary: str,
) -> DownloadedFootage:
    url = candidate.download_url
    if not url:
        raise FootageAssetDownloadError("DOWNLOAD_URL_MISSING", "Candidate has no download URL.")
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    try:
        valid_https = parsed.scheme == "https" and parsed.port in (None, 443)
    except ValueError:
        valid_https = False
    if not valid_https or parsed.username or parsed.password or parsed.fragment:
        raise FootageAssetDownloadError(
            "DOWNLOAD_URL_REJECTED",
            "Candidate download URL did not pass public HTTPS checks.",
        )
    allowed_domains = {
        "pexels": ("pexels.com",),
        "pixabay": ("pixabay.com",),
    }.get(candidate.provider)
    if not allowed_domains or not any(
        host == domain or host.endswith(f".{domain}") for domain in allowed_domains
    ):
        raise FootageAssetDownloadError(
            "DOWNLOAD_HOST_REJECTED", "Candidate download host is not allowed for its provider."
        )
    try:
        pinned_ip = _resolve_public_ip(host)
    except (OSError, ValueError) as exc:
        raise FootageAssetDownloadError(
            "DOWNLOAD_URL_REJECTED", "Candidate download URL did not pass public HTTPS checks."
        ) from exc

    destination_directory.mkdir(parents=True, exist_ok=True)
    destination = destination_directory / f"{candidate.provider}-{uuid.uuid4().hex}.mp4"
    digest = hashlib.sha256()
    total = 0
    connection = None
    try:
        connection = _PinnedHTTPSConnection(host, pinned_ip, timeout=timeout_seconds)
        request_path = parsed.path or "/"
        if parsed.query:
            request_path += f"?{parsed.query}"
        try:
            connection.request(
                "GET",
                request_path,
                headers={"Host": host, "Accept": "video/*", "Connection": "close"},
            )
            response = connection.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            raise FootageAssetDownloadError(
                "DOWNLOAD_NETWORK_FAILED",
                "The selected footage could not be downloaded securely.",
                retryable=True,
            ) from exc
        if response.status != 200:
            raise FootageAssetDownloadError(
                "DOWNLOAD_HTTP_FAILED",
                "The selected footage URL returned an unsuccessful response.",
                retryable=response.status >= 500 or response.status == 429,
            )
        content_type = response.getheader("Content-Type", "").split(";", 1)[0].lower()
        if content_type and not content_type.startswith("video/") and content_type != "application/octet-stream":
            raise FootageAssetDownloadError(
                "DOWNLOAD_CONTENT_TYPE_INVALID",
                "The selected footage URL did not return video content.",
            )
        content_length = response.getheader("Content-Length")
        try:
            if content_length is not None and int(content_length) > max_bytes:
                raise FootageAssetDownloadError(
                    "DOWNLOAD_TOO_LARGE",
                    "The selected footage exceeds the configured size limit.",
                )
        except ValueError as exc:
            raise FootageAssetDownloadError(
                "DOWNLOAD_CONTENT_LENGTH_INVALID", "The footage content length is invalid."
            ) from exc
        with destination.open("xb") as output:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise FootageAssetDownloadError(
                        "DOWNLOAD_TOO_LARGE",
                        "The selected footage exceeds the configured size limit.",
                    )
                digest.update(chunk)
                try:
                    output.write(chunk)
                except OSError as exc:
                    raise FootageAssetDownloadError(
                        "FOOTAGE_STORAGE_FAILED",
                        "The validated footage could not be stored.",
                    ) from exc
        if total == 0:
            raise FootageAssetDownloadError("DOWNLOAD_EMPTY", "The selected footage download is empty.")
        try:
            probe = subprocess.run(
                [
                    ffprobe_binary,
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=codec_type,width,height",
                    "-of",
                    "json",
                    str(destination),
                ],
                capture_output=True,
                check=False,
                timeout=min(timeout_seconds, 60.0),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise FootageAssetDownloadError(
                "FOOTAGE_VALIDATOR_UNAVAILABLE",
                "FFprobe could not validate the selected footage.",
                retryable=isinstance(exc, FileNotFoundError),
            ) from exc
        if probe.returncode != 0:
            raise FootageAssetDownloadError(
                "CORRUPTED_FOOTAGE_DOWNLOAD",
                "Downloaded footage failed media validation.",
                retryable=False,
            )
        try:
            streams = json.loads(probe.stdout).get("streams", [])
            video_stream = next(
                stream for stream in streams if stream.get("codec_type") == "video"
            )
            width = int(video_stream["width"])
            height = int(video_stream["height"])
        except (ValueError, TypeError, KeyError, StopIteration, json.JSONDecodeError) as exc:
            raise FootageAssetDownloadError(
                "CORRUPTED_FOOTAGE_DOWNLOAD",
                "Downloaded footage has invalid video metadata.",
            ) from exc
        if width <= 0 or height <= 0:
            raise FootageAssetDownloadError(
                "CORRUPTED_FOOTAGE_DOWNLOAD",
                "Downloaded footage has invalid video dimensions.",
            )
        return DownloadedFootage(destination, total, digest.hexdigest(), width, height)
    except FootageAssetDownloadError:
        destination.unlink(missing_ok=True)
        raise
    except (ssl.SSLError, socket.timeout, ConnectionError, http.client.HTTPException) as exc:
        destination.unlink(missing_ok=True)
        raise FootageAssetDownloadError(
            "DOWNLOAD_NETWORK_FAILED",
            "The selected footage could not be downloaded securely.",
            retryable=True,
        ) from exc
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise FootageAssetDownloadError(
            "FOOTAGE_STORAGE_FAILED", "The validated footage could not be stored."
        ) from exc
    finally:
        if connection is not None:
            connection.close()


def _resolve_public_ip(host: str) -> str:
    records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    addresses = list(
        dict.fromkeys(
            ipaddress.ip_address(record[4][0])
            for record in records
        )
    )
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("Provider CDN must resolve only to public IP addresses.")
    return str(addresses[0])
