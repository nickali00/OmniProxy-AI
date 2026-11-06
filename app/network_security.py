"""When HTTPS is required, cleartext upstreams are restricted to local networks."""

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from app.config import settings
from app.errors import GatewayError


async def enforce_upstream_transport(url):
    if not settings.require_https:
        return
    parsed = urlsplit(str(url))
    if parsed.scheme == "https":
        return
    if parsed.scheme != "http" or not parsed.hostname:
        raise GatewayError(503, "Invalid upstream transport.", code="upstream_transport_rejected")
    try:
        answers = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, parsed.port or 80, type=socket.SOCK_STREAM)
        addresses = {ipaddress.ip_address(answer[4][0].split("%", 1)[0]) for answer in answers}
    except (OSError, ValueError):
        raise GatewayError(503, "Upstream destination cannot be verified.", code="upstream_transport_rejected") from None
    if not addresses or any(not (address.is_private or address.is_loopback) or address.is_link_local or address.is_unspecified or address.is_multicast or address.is_reserved for address in addresses):
        raise GatewayError(503, "Public upstreams must use HTTPS.", code="upstream_https_required")
