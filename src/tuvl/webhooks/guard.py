"""Where a webhook may be sent. Declared URLs still come from the environment, which can change, and
a name can resolve to an inside address, so the destination is checked twice: the text of the URL
when it is declared, and every address the name resolves to just before connecting."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

IP = ipaddress.IPv4Address | ipaddress.IPv6Address

_EXTRA_BLOCKED = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "100.64.0.0/10",  # carrier-grade NAT
        "198.18.0.0/15",  # benchmarking
        "192.0.0.0/24",
        "240.0.0.0/4",
        "64:ff9b::/96",  # NAT64: embeds an IPv4 address
        "2002::/16",  # 6to4: embeds an IPv4 address
    )
)
#: where a cloud provider answers with the instance's credentials; no destination is ever worth that
_METADATA = tuple(
    ipaddress.ip_network(n) for n in ("169.254.0.0/16", "fd00:ec2::/32", "100.100.100.200/32")
)
_INTERNAL_SUFFIXES = (".local", ".localhost", ".internal", ".intranet", ".lan", ".home", ".corp")


class BlockedAddress(Exception):  # noqa: N818
    """The destination is an internal address."""


def _embedded(ip: ipaddress.IPv6Address) -> IP | None:
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    packed = ip.packed
    if packed[:12] == bytes.fromhex("0064ff9b0000000000000000"):  # 64:ff9b::/96
        return ipaddress.IPv4Address(packed[12:])
    return None


def is_metadata(ip: IP) -> bool:
    """True for a cloud provider's instance-metadata address, however it is written."""
    if isinstance(ip, ipaddress.IPv6Address):
        inner = _embedded(ip)
        if inner is not None and is_metadata(inner):
            return True
    return any(ip in net for net in _METADATA)


def is_blocked(ip: IP) -> bool:
    """True for any address that is not a plain public unicast address."""
    if isinstance(ip, ipaddress.IPv6Address):
        inner = _embedded(ip)
        if inner is not None and is_blocked(inner):
            return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or any(ip in net for net in _EXTRA_BLOCKED)
    )


def _literal_ip(host: str) -> IP | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def url_problem(url: str, *, allow_private: bool = False) -> str | None:
    """Why *url* may not be a webhook target, judged from its text alone, or ``None``."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "not a valid URL"
    host = parts.hostname or ""
    if parts.scheme not in ("https", "http") or not host:
        return "must be an https:// URL with a host"
    if parts.scheme == "http" and not allow_private:
        return "must be https (http needs allow_private: true)"
    if parts.username is not None or parts.password is not None:
        return "must not carry credentials (user:pass@)"
    if port is not None and not 1 <= port <= 65535:
        return "port out of range"
    if allow_private:
        return None
    literal = _literal_ip(host)
    if literal is not None:
        return "points at an internal address" if is_blocked(literal) else None
    try:
        socket.inet_aton(host)
    except OSError:
        pass
    else:
        return "uses a non-canonical IP address form"
    if host.isdigit() or host.lower().startswith("0x"):
        return "uses a non-canonical IP address form"
    lowered = host.lower().rstrip(".")
    if lowered == "localhost" or lowered.endswith(_INTERNAL_SUFFIXES) or "." not in lowered:
        return "points at an internal host name"
    return None


async def resolve(host: str, port: int, *, allow_private: bool = False) -> list[str]:
    """Every address *host* resolves to, refusing the lot if any one is internal (a name that
    answers with a mix is how a rebinding attack gets through)."""
    literal = _literal_ip(host)
    if literal is not None:
        addresses = [str(literal)]
    else:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
        )
        addresses = list(dict.fromkeys(str(info[4][0]) for info in infos))
    if not addresses:
        raise OSError("the host name did not resolve")
    for address in addresses:
        ip = _literal_ip(address.split("%", 1)[0])
        if ip is None:
            raise BlockedAddress("the host resolves to something that is not an address")
        if is_metadata(ip):
            raise BlockedAddress("the host resolves to a cloud-metadata address")
        if not allow_private and is_blocked(ip):
            raise BlockedAddress("the host resolves to an internal address")
    return addresses
