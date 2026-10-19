"""Validation and bounded fetching for untrusted remote audio URLs."""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from pathlib import Path
from urllib.parse import SplitResult, urljoin, urlsplit

from scenepilot.errors import ApiError


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, pinned_ip: str, *, timeout: float) -> None:
        super().__init__(
            hostname,
            port=443,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self.pinned_ip = pinned_ip

    def connect(self) -> None:
        sock = socket.create_connection(
            (self.pinned_ip, self.port),
            self.timeout,
            self.source_address,
        )
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _validate_and_resolve(
    url: str,
    *,
    allow_hosts: frozenset[str],
    resolver=socket.getaddrinfo,
) -> tuple[SplitResult, str, list[str]]:
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() != "https":
            raise ValueError("HTTPS is required")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("Credentials and fragments are not allowed")
        if not parsed.hostname or parsed.port not in (None, 443):
            raise ValueError("A public HTTPS host on port 443 is required")
        host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        if not host or any(character in host for character in "/\\"):
            raise ValueError("Invalid hostname")
        if allow_hosts and host not in allow_hosts:
            raise ValueError("The host is not on the configured allowlist")
        try:
            literal_ip = ipaddress.ip_address(host)
        except ValueError:
            literal_ip = None
        if literal_ip is not None:
            addresses = [literal_ip]
        else:
            records = resolver(host, 443, type=socket.SOCK_STREAM)
            addresses = [ipaddress.ip_address(record[4][0]) for record in records]
        if not addresses:
            raise ValueError("The hostname did not resolve")
        if any(
            not address.is_global
            or address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
            for address in addresses
        ):
            raise ValueError("The URL must resolve exclusively to public IP addresses")
    except (IndexError, TypeError, ValueError, UnicodeError, OSError) as exc:
        raise ApiError(
            422,
            "UNSAFE_AUDIO_URL",
            "The audio URL must be an HTTPS URL resolving only to public addresses.",
            details={"reason": str(exc)},
        ) from exc
    return parsed, host, [str(address) for address in addresses]


def validate_public_https_url(
    url: str,
    *,
    allow_hosts: frozenset[str],
    resolver=socket.getaddrinfo,
) -> str:
    _validate_and_resolve(url, allow_hosts=allow_hosts, resolver=resolver)
    return url


def fetch_public_audio(
    url: str,
    *,
    destination,
    max_bytes: int,
    timeout_seconds: float,
    max_redirects: int,
    allow_hosts: frozenset[str],
) -> int:
    """Download a public HTTPS URL through an IP pinned after public-address validation."""
    current_url = url
    download_created = False
    for redirect_count in range(max_redirects + 1):
        parsed, host, public_addresses = _validate_and_resolve(
            current_url,
            allow_hosts=allow_hosts,
        )
        connection = _PinnedHTTPSConnection(
            host,
            public_addresses[0],
            timeout=timeout_seconds,
        )
        try:
            request_path = parsed.path or "/"
            if parsed.query:
                request_path += f"?{parsed.query}"
            host_header = f"[{host}]" if ":" in host else host
            connection.request(
                "GET",
                request_path,
                headers={
                    "Host": host_header,
                    "Accept": "audio/*",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location or redirect_count == max_redirects:
                    raise ApiError(
                        422,
                        "AUDIO_URL_REDIRECT_REJECTED",
                        "The audio URL redirected too many times or omitted its target.",
                    )
                current_url = urljoin(current_url, location)
                continue
            if response.status != 200:
                raise ApiError(
                    422,
                    "AUDIO_URL_FETCH_FAILED",
                    "The audio URL returned an unsuccessful response.",
                    retryable=response.status >= 500 or response.status == 429,
                    details={"upstream_status": response.status},
                )
            content_length = response.getheader("Content-Length")
            if content_length:
                try:
                    declared_size = int(content_length)
                except ValueError as exc:
                    raise ApiError(
                        422,
                        "AUDIO_URL_FETCH_FAILED",
                        "The remote audio returned an invalid content length.",
                    ) from exc
                if declared_size < 0:
                    raise ApiError(
                        422,
                        "AUDIO_URL_FETCH_FAILED",
                        "The remote audio returned an invalid content length.",
                    )
                if declared_size > max_bytes:
                    raise ApiError(
                        413,
                        "UPLOAD_TOO_LARGE",
                        "The remote audio exceeds the configured upload limit.",
                        details={"max_upload_bytes": max_bytes},
                    )
            total = 0
            with Path(destination).open("xb") as output:
                download_created = True
                while chunk := response.read(min(64 * 1024, max_bytes - total + 1)):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ApiError(
                            413,
                            "UPLOAD_TOO_LARGE",
                            "The remote audio exceeds the configured upload limit.",
                            details={"max_upload_bytes": max_bytes},
                        )
                    output.write(chunk)
            if total == 0:
                raise ApiError(422, "EMPTY_AUDIO", "The remote audio file is empty.")
            return total
        except ApiError:
            if download_created:
                Path(destination).unlink(missing_ok=True)
            raise
        except (http.client.HTTPException, OSError, ssl.SSLError) as exc:
            if download_created:
                Path(destination).unlink(missing_ok=True)
            raise ApiError(
                502,
                "AUDIO_URL_FETCH_FAILED",
                "The remote audio could not be downloaded.",
                retryable=True,
                details={"reason": type(exc).__name__},
            ) from exc
        finally:
            connection.close()
    raise ApiError(422, "AUDIO_URL_REDIRECT_REJECTED", "The audio URL redirect limit was exceeded.")
