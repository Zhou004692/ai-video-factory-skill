"""Safe, credential-free downloads for provider-returned media URLs."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx


_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def _assert_public_address(value: str) -> None:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError as exc:
        raise ValueError("Provider media URL did not resolve to a public address") from exc
    if not address.is_global:
        raise ValueError("Provider media URL is not a public HTTPS address")


def _validated_targets(url: str) -> tuple[str, list[str], str, str]:
    """Return logical URL, ordered IP targets, Host header and TLS hostname."""
    normalized = str(url or "").strip()
    parsed = urlsplit(normalized)
    if parsed.scheme.casefold() != "https":
        raise ValueError("Provider media URL must use HTTPS")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("Provider media URL is not safe")
    host = parsed.hostname.rstrip(".").casefold()
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("Provider media URL is not a public HTTPS address")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Provider media URL port is not safe") from exc
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        literal_host = False
        ascii_host = host.encode("idna").decode("ascii")
        try:
            records = socket.getaddrinfo(
                ascii_host, port or 443, type=socket.SOCK_STREAM
            )
        except OSError as exc:
            raise ValueError(
                "Provider media URL host could not be safely resolved"
            ) from exc
        if not records:
            raise ValueError("Provider media URL host did not resolve publicly")
        addresses = []
        seen_addresses: set[tuple[int, bytes]] = set()
        for record in records:
            address = str(record[4][0])
            _assert_public_address(address)
            parsed_address = ipaddress.ip_address(address.split("%", 1)[0])
            identity = (parsed_address.version, parsed_address.packed)
            if identity not in seen_addresses:
                addresses.append(parsed_address)
                seen_addresses.add(identity)
        # Do not let platform-specific resolver ordering turn failover into a
        # race. Prefer IPv6, then IPv4, with stable numeric ordering per family.
        addresses.sort(key=lambda item: (
            0 if item.version == 6 else 1, item.packed,
        ))
    else:
        literal_host = True
        _assert_public_address(str(literal))
        addresses = [literal]
        ascii_host = host
    original_host = (
        f"[{ascii_host}]"
        if literal_host and addresses[0].version == 6 else ascii_host
    )
    host_header = original_host + (
        f":{port}" if port is not None and port != 443 else ""
    )
    pinned_urls = []
    for address in addresses:
        pinned_host = f"[{address}]" if address.version == 6 else str(address)
        pinned_netloc = pinned_host + (
            f":{port}" if port is not None else ""
        )
        pinned_urls.append(urlunsplit((
            "https", pinned_netloc, parsed.path or "/", parsed.query, "",
        )))
    return normalized, pinned_urls, host_header, ascii_host


def validate_url(url: str) -> str:
    """Require HTTPS and resolve every host only to public addresses."""
    return _validated_targets(url)[0]


async def download_bytes(
    url: str, *, timeout: float = 120, max_redirects: int = 5
) -> tuple[bytes, str]:
    """Download without provider credentials, validating every redirect hop."""
    current = str(url or "").strip()
    for hop in range(max_redirects + 1):
        current, pinned_urls, host_header, sni_hostname = _validated_targets(
            current
        )
        response = None
        last_connect_error = None
        for pinned_url in pinned_urls:
            try:
                async with httpx.AsyncClient(
                    timeout=timeout,
                    follow_redirects=False,
                    headers={},
                    trust_env=False,
                ) as client:
                    response = await client.get(
                        pinned_url,
                        headers={"Host": host_header},
                        extensions={"sni_hostname": sni_hostname},
                    )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_connect_error = exc
                continue
            break
        if response is None:
            if last_connect_error is not None:
                raise last_connect_error
            raise ValueError("Provider media URL has no safe connection target")
        if response.status_code not in _REDIRECT_STATUSES:
            response.raise_for_status()
            return response.content, current
        if hop >= max_redirects:
            raise ValueError("Provider media URL exceeded safe redirect limit")
        location = response.headers.get("location", "").strip()
        if not location:
            raise ValueError("Provider media redirect is missing a safe location")
        current = urljoin(current, location)
    raise ValueError("Provider media download could not be completed safely")
