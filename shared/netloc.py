"""netloc — the single owner of the "host:port" listen-address format.

Every listen parse in the repo converges here: 本地代理 (proxy.py),
Suanpan gateway (both launch paths: suanpan_runtime + suanpan/main.py),
merge_config validation, 系统代理 targeting, app port probing, and the
config_server derived port fields.

Deliberately owns ONLY the format. Defaults stay with their owners
(suanpan/config.py schema for the gateway, config.DEFAULT_CONFIG for the
local proxy).
"""
import ipaddress

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def parse_listen(text, default_port=None):
    """Parse "host:port" → (host, port). Raises ValueError on bad input.

    An empty host normalizes to 127.0.0.1 (":9527" reads as loopback).
    IPv6 with a port must be bracketed ("[::1]:9527"); a bare IPv6
    literal ("::1") is host-only (#40) — "::1:9527" is itself a valid
    IPv6 address, so it cannot carry a port. Port must be 1-65535.
    A bare host with no port is accepted when default_port is given.
    """
    if not isinstance(text, str):
        raise ValueError(f"listen must be a string, got {type(text).__name__}")
    text = text.strip()
    if text.startswith("["):
        host, sep, rest = text.partition("]")
        host = host[1:].strip()
        if not sep:
            raise ValueError(f"unterminated IPv6 bracket in {text!r}")
        if not rest:
            if default_port is None:
                raise ValueError(f"listen port missing in {text!r}")
            port = default_port
        elif rest.startswith(":") and rest[1:].isdigit():
            port = int(rest[1:])
        else:
            raise ValueError(f"invalid listen port in {text!r}")
    else:
        try:
            ipaddress.IPv6Address(text)
        except ValueError:
            if default_port is not None and ":" not in text:
                text = f"{text}:{default_port}"
            host, _sep, port_s = text.rpartition(":")
            host = host.strip()
            if ":" in host:
                raise ValueError(
                    f"unbracketed IPv6 must use brackets: {text!r}")
            if not port_s.isdigit():
                raise ValueError(f"invalid listen port in {text!r}")
            port = int(port_s)
        else:
            # Bare IPv6 literal — host only, no port separator (#40).
            if default_port is None:
                raise ValueError(f"listen port missing in {text!r}")
            return text, default_port
    if not host:
        host = "127.0.0.1"
    if not 1 <= port <= 65535:
        raise ValueError(f"listen port out of range (1-65535): {port}")
    return host, port


def require_loopback(host):
    """Raise ValueError unless host is a loopback address."""
    if host not in LOOPBACK_HOSTS:
        raise ValueError(f"only loopback listeners are permitted, got: {host!r}")


def format_listen(host, port):
    """Build "host:port", bracketing IPv6 hosts."""
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
