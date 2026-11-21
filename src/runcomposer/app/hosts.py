"""The host names the server answers to (``core.api.hosts``, DESIGN.md §8).

A browser sends, in ``Host``, the name it reached the server by. A page on
a name its author controls can be made to resolve to the server's address
(DNS rebinding); to the browser that page and the server are then one
origin, and comparing ``Origin`` with ``Host`` cannot tell them apart. A
server that answers only to its own names refuses such a request before
anything else looks at it.

An entry is a host name, an IPv4 address or a bracketed IPv6 address
(``[::1]``), optionally with ``:port``:

- ``runcomposer.shop.example`` — that name, on any port;
- ``runcomposer.shop.example:8443`` — that name on that port only (a
  ``Host`` without a port means the scheme's default port);
- ``*.shop.example`` — every name below ``shop.example`` (not
  ``shop.example`` itself);
- ``*`` — every name: the same as leaving the setting out.

Unset, the server answers to every name — unless it is served on a loopback
address: then only the loopback names (:data:`LOOPBACK_NAMES`, and the bound
address itself) are answered, since nothing but this machine can reach it by
any other.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

__all__ = [
    "LOOPBACK_NAMES",
    "HostPattern",
    "effective_hosts",
    "is_loopback",
    "parse_host",
    "parse_host_pattern",
]

#: What a server bound to a loopback address answers to when
#: ``core.api.hosts`` is unset — on any port.
LOOPBACK_NAMES = ("localhost", "127.0.0.1", "[::1]")


@dataclass(frozen=True)
class HostPattern:
    """One parsed entry. ``name`` is lower case, an IPv6 address without its
    brackets; ``wildcard`` means "any name ending in ``.name``"; ``port``
    ``None`` means any port."""

    name: str
    port: int | None = None
    wildcard: bool = False
    any_host: bool = False

    def matches(self, name: str, port: int) -> bool:
        if self.port is not None and self.port != port:
            return False
        if self.any_host:
            return True
        if self.wildcard:
            return name.endswith("." + self.name)
        return name == self.name


def _split(text: str) -> tuple[str, str]:
    """``(name, port text)`` of ``name[:port]`` or ``[v6][:port]``; the port
    text is empty when there is none. Raises ``ValueError``."""
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            raise ValueError("an opening '[' without its ']'")
        name, rest = text[1:end], text[end + 1 :]
        if rest and not rest.startswith(":"):
            raise ValueError("text after ']' that is not ':port'")
        return name, rest[1:]
    name, _, port = text.partition(":")
    if ":" in port:
        raise ValueError("an IPv6 address must be written in brackets, as in '[::1]'")
    return name, port


def _port(text: str) -> int:
    if not text.isdigit() or not 0 < int(text) < 65536:
        raise ValueError(f"port {text!r} is not a number from 1 to 65535")
    return int(text)


def parse_host_pattern(entry: str) -> HostPattern:
    """One ``core.api.hosts`` entry. Raises ``ValueError`` saying what is
    wrong with it."""
    text = entry.strip().lower()
    if not text:
        raise ValueError("an empty entry")
    if text == "*":
        return HostPattern(name="*", any_host=True)
    name, port_text = _split(text)
    port = _port(port_text) if port_text else None
    wildcard = name.startswith("*.")
    if wildcard:
        name = name[2:]
    if not name or "*" in name:
        raise ValueError("'*' is allowed only as the whole entry or as a leading '*.'")
    if any(char.isspace() or char in "/?#@" for char in name):
        raise ValueError(f"{name!r} is not a host name")
    return HostPattern(name=name, port=port, wildcard=wildcard)


def parse_host(value: str | None, default_port: int) -> tuple[str, int] | None:
    """``(name, port)`` of a ``Host`` header value — ``None`` when it is
    missing or not a host. ``default_port`` stands in for a missing port."""
    if not value:
        return None
    try:
        name, port_text = _split(value.strip().lower())
        port = _port(port_text) if port_text else default_port
    except ValueError:
        return None
    if not name or any(char.isspace() or char in "/?#@*" for char in name):
        return None
    return name, port


def is_loopback(bind_host: str) -> bool:
    """Whether ``bind_host`` — the address a server listens on — reaches
    this machine only: ``localhost`` or a loopback address."""
    text = bind_host.strip().lower().strip("[]")
    if text == "localhost":
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def effective_hosts(
    configured: Sequence[str] | None, bind_host: str | None
) -> tuple[HostPattern, ...] | None:
    """What the server answers to: the configured entries; when none are
    configured and it is bound to a loopback address, the loopback names and
    that address; otherwise ``None`` — every name."""
    entries: Iterable[str]
    if configured is not None:
        entries = configured
    elif bind_host is not None and is_loopback(bind_host):
        bound = bind_host.strip().lower()
        if ":" in bound and not bound.startswith("["):
            bound = f"[{bound}]"
        entries = dict.fromkeys([*LOOPBACK_NAMES, bound])
    else:
        return None
    patterns = tuple(parse_host_pattern(str(entry)) for entry in entries)
    return None if any(pattern.any_host for pattern in patterns) else patterns
