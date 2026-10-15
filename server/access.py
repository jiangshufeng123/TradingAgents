"""Who may reach the service, and where it can be reached from.

When the service listens beyond this computer (``--host 0.0.0.0``), requests are
accepted only from the networks in ``TRADINGAGENTS_ALLOWED_NETS`` (comma-separated
CIDRs). The default is this computer, the home network (private ranges) and
Tailscale (100.64.0.0/10), so an exposed port still refuses the internet even if
the firewall is misconfigured. Behind a reverse proxy, the proxy's address is
what the service sees, and the proxy decides; see docs/deploy.md.
"""

from __future__ import annotations

import ipaddress
import os
import socket

DEFAULT_NETS = ("127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                "100.64.0.0/10", "fd7a:115c:a1e0::/48", "fe80::/10")
TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")


def allowed_networks(text: str | None = None) -> list:
    raw = text if text is not None else os.environ.get("TRADINGAGENTS_ALLOWED_NETS", "")
    items = [x.strip() for x in raw.split(",") if x.strip()] or list(DEFAULT_NETS)
    nets = []
    for item in items:
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError as exc:
            raise ValueError(f"TRADINGAGENTS_ALLOWED_NETS: {item!r} is not a network ({exc})") from None
    return nets


def is_allowed(address: str | None, nets) -> bool:
    if not address:
        return False
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return address in ("testclient", "localhost")       # the test client has no IP
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip.version == n.version and ip in n for n in nets)


def local_addresses() -> list[tuple[str, str]]:
    """This computer's IPv4 addresses as (address, kind): kind is tailscale, lan or other."""
    found = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    try:   # the address used for outgoing traffic (no packet is sent)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            found.add(s.getsockname()[0])
    except OSError:
        pass
    out = []
    for a in sorted(found):
        ip = ipaddress.ip_address(a)
        if ip.is_loopback or ip.is_link_local:
            continue
        kind = "tailscale" if ip in TAILSCALE_V4 else "lan" if ip.is_private else "other"
        out.append((a, kind))
    return out
