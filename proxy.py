"""Async HTTP→SOCKS5 proxy with SSH tunnel management."""
import asyncio
import logging
import os
import socket
import struct
import subprocess
import threading
import time
from urllib.parse import urlsplit

from stats import Stats
import host_key
import netloc
from subprocess_monitor import SubprocessMonitor
from async_runtime import AsyncRuntime

logger = logging.getLogger("magic-proxy.proxy")

SOCKS5_TIMEOUT = 15            # seconds for SOCKS5 handshake
RELAY_BUF = 65536              # bytes per read
DRAIN_THRESHOLD = 256 * 1024   # bytes between explicit drains
PORT_PROBE_TIMEOUT = 0.1       # seconds for connecting-state probe
CLIENT_HEADER_TIMEOUT = 30     # seconds; protects against slowloris clients
MAX_HEADER_BYTES = 64 * 1024   # applies to request line and all headers
RELAY_IDLE_TIMEOUT = 300       # seconds without bytes in either direction
MAX_CLIENT_CONNECTIONS = 256


async def _read_headers(reader):
    """Read bounded HTTP headers, returning raw lines including no terminator."""
    lines = []
    size = 0
    while True:
        line = await asyncio.wait_for(reader.readline(), timeout=CLIENT_HEADER_TIMEOUT)
        size += len(line)
        if size > MAX_HEADER_BYTES or len(line) > MAX_HEADER_BYTES:
            raise ValueError("HTTP headers exceed limit")
        if line in (b"\r\n", b""):
            return lines
        lines.append(line)


def _parse_authority(authority, default_port=None):
    """Parse a HTTP authority, including bracketed IPv6, with a safe port."""
    authority = authority.strip()
    if authority.startswith("["):
        host, sep, remainder = authority[1:].partition("]")
        if not sep:
            raise ValueError("invalid IPv6 authority")
        port_text = remainder[1:] if remainder.startswith(":") else None
    else:
        host, sep, port_text = authority.rpartition(":")
        if not sep:
            host, port_text = authority, None
        elif not host or ":" in host:  # unbracketed IPv6 is not valid HTTP authority
            raise ValueError("invalid authority")
    if not host:
        raise ValueError("empty host")
    port = default_port if port_text in (None, "") else int(port_text)
    if port is None or not 1 <= port <= 65535:
        raise ValueError("invalid port")
    return host, port


async def _close_writer(writer):
    if writer is None:
        return
    try:
        writer.close()
        wait_closed = getattr(writer, "wait_closed", None)
        if wait_closed:
            await asyncio.wait_for(wait_closed(), timeout=1)
    except Exception:
        pass


async def socks5_connect(host: str, port: int, socks_addr: str):
    """Establish a SOCKS5 connection (no auth) through the tunnel."""
    socks_host, socks_port = socks_addr.split(":")
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(socks_host, int(socks_port)),
        timeout=SOCKS5_TIMEOUT,
    )

    try:
        async def handshake():
            writer.write(b"\x05\x01\x00")
            await writer.drain()
            resp = await reader.readexactly(2)
            if resp != b"\x05\x00":
                raise RuntimeError(f"SOCKS5 handshake rejected: {resp.hex()}")

            try:
                ip = socket.inet_pton(socket.AF_INET, host)
                addr_type = b"\x01"
                addr = ip
            except OSError:
                try:
                    addr = socket.inet_pton(socket.AF_INET6, host)
                    addr_type = b"\x04"
                except OSError:
                    encoded = host.encode("idna")
                    if len(encoded) > 255:
                        raise ValueError("SOCKS5 hostname exceeds 255 bytes")
                    addr_type = b"\x03"
                    addr = len(encoded).to_bytes(1, "big") + encoded

            writer.write(b"\x05\x01\x00" + addr_type + addr + struct.pack("!H", port))
            await writer.drain()

            header = await reader.readexactly(4)
            if header[0] != 0x05 or header[1] != 0x00:
                raise RuntimeError(f"SOCKS5 connect failed: status={header[1]}")

            atyp = header[3]
            if atyp == 0x01:
                await reader.readexactly(6)
            elif atyp == 0x03:
                length = (await reader.readexactly(1))[0]
                await reader.readexactly(length + 2)
            elif atyp == 0x04:
                await reader.readexactly(18)
            else:
                raise RuntimeError(f"SOCKS5 unknown address type: {atyp}")

        await asyncio.wait_for(handshake(), timeout=SOCKS5_TIMEOUT)
        return reader, writer
    except Exception:
        await _close_writer(writer)
        raise


async def bidirectional_relay(ra, wa, rb, wb, stats):
    """Half-close-aware relay: each direction drains independently, then EOFs."""
    async def relay(src, dst, record):
        bytes_since_drain = 0
        try:
            while True:
                data = await asyncio.wait_for(src.read(RELAY_BUF), timeout=RELAY_IDLE_TIMEOUT)
                if not data:
                    break
                dst.write(data)
                record(len(data))
                bytes_since_drain += len(data)
                if bytes_since_drain >= DRAIN_THRESHOLD:
                    await dst.drain()
                    bytes_since_drain = 0
            try:
                await dst.drain()
            except Exception:
                pass
        except (ConnectionError, OSError, asyncio.TimeoutError, asyncio.CancelledError):
            pass
        finally:
            try:
                if dst.can_write_eof():
                    dst.write_eof()
            except Exception:
                pass

    await asyncio.gather(
        relay(ra, wb, stats.record_up),
        relay(rb, wa, stats.record_down),
        return_exceptions=True,
    )
    await asyncio.gather(_close_writer(wa), _close_writer(wb), return_exceptions=True)


async def handle_connect(client_reader, client_writer, host, port, socks_addr, stats):
    """Handle HTTP CONNECT (HTTPS) tunneling."""
    stats.inc_connections()
    remote_writer = None
    try:
        await _read_headers(client_reader)

        try:
            remote_reader, remote_writer = await socks5_connect(host, port, socks_addr)
        except Exception as e:
            logger.error("SOCKS5 connect to %s:%s failed: %s", host, port, e)
            client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await client_writer.drain()
            client_writer.close()
            return

        client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await client_writer.drain()
        await bidirectional_relay(client_reader, client_writer, remote_reader, remote_writer, stats)
    finally:
        await _close_writer(remote_writer)
        stats.dec_connections()


async def handle_http(client_reader, client_writer, request_line, socks_addr, stats):
    """Handle plain HTTP request via SOCKS5."""
    stats.inc_connections()
    remote_writer = None
    try:
        method, url, _ = request_line.decode().split(maxsplit=2)
        parsed = urlsplit(url)
        if parsed.scheme.lower() != "http" or not parsed.hostname:
            raise ValueError("plain HTTP proxy requests must use an absolute http URL")
        host, port = parsed.hostname, parsed.port or 80

        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        headers = await _read_headers(client_reader)
        # Proxy credentials and hop-by-hop headers belong only to this local
        # proxy. Forwarding them exposes credentials to the destination.
        connection_tokens = set()
        for line in headers:
            name, sep, value = line.decode("iso-8859-1", "replace").partition(":")
            if sep and name.strip().lower() == "connection":
                connection_tokens.update(v.strip().lower() for v in value.split(","))
        # Keep Transfer-Encoding unless it was explicitly nominated by
        # Connection: the body is relayed byte-for-byte, so stripping the
        # normal chunked framing header would corrupt uploads.
        blocked = {"proxy-authorization", "proxy-connection", "connection", "keep-alive",
                   "te", "trailer", "upgrade"} | connection_tokens
        forwarded_headers = []
        for line in headers:
            name, sep, _ = line.decode("iso-8859-1", "replace").partition(":")
            if sep and name.strip().lower() not in blocked:
                forwarded_headers.append(line)

        try:
            remote_reader, remote_writer = await socks5_connect(host, port, socks_addr)
        except Exception as e:
            logger.error("SOCKS5 connect to %s:%s failed: %s", host, port, e)
            client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await client_writer.drain()
            client_writer.close()
            return

        remote_writer.write(f"{method} {path} HTTP/1.1\r\n".encode())
        for line in forwarded_headers:
            remote_writer.write(line)

        remote_writer.write(b"\r\n")
        await remote_writer.drain()
        await bidirectional_relay(client_reader, client_writer, remote_reader, remote_writer, stats)
    finally:
        await _close_writer(remote_writer)
        stats.dec_connections()


async def handle_client(client_reader, client_writer, socks_addr, stats):
    """Process one HTTP proxy client."""
    try:
        request_line = await asyncio.wait_for(
            client_reader.readline(), timeout=CLIENT_HEADER_TIMEOUT,
        )
        if len(request_line) > MAX_HEADER_BYTES:
            raise ValueError("HTTP request line exceeds limit")
        if not request_line:
            client_writer.close()
            return

        parts = request_line.decode(errors="replace").split()
        if len(parts) < 2:
            client_writer.close()
            return
        method, target = parts[0], parts[1]

        if method == "CONNECT":
            host, port = _parse_authority(target)
            await handle_connect(client_reader, client_writer, host, port, socks_addr, stats)
        else:
            await handle_http(client_reader, client_writer, request_line, socks_addr, stats)
    except asyncio.CancelledError:
        raise
    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError) as e:
        # Client closed mid-stream — normal browser/curl behavior, not an error.
        logger.debug("client disconnected: %s", e)
        try:
            client_writer.close()
        except Exception:
            pass
    except (ValueError, asyncio.TimeoutError) as e:
        logger.info("invalid or stalled client request: %s", e)
        try:
            client_writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            await client_writer.drain()
            client_writer.close()
        except Exception:
            pass
    except Exception:
        logger.exception("Error handling client")
        try:
            client_writer.close()
        except Exception:
            pass


class SSHMonitor(SubprocessMonitor):
    """Manages an SSH SOCKS5 tunnel subprocess."""

    _PROCESS_NAME = "SSH"
    _STATUS_STARTING = "connecting"
    _STATUS_RUNNING = "connected"

    def __init__(self, *, line_sink=None):
        super().__init__(line_sink=line_sink)
        self._current_name = ""

    @property
    def current_name(self) -> str:
        return self._current_name

    @property
    def is_host_key_changed(self) -> bool:
        """True when SSH exited because the server's host key changed.

        Classifies the raw stderr so callers don't string-match SSH output.
        """
        return (self._status == "error"
                and "REMOTE HOST IDENTIFICATION HAS CHANGED" in self._error_msg)

    def start(self, tunnel: dict, socks5_port: int, password: str = ""):
        """Start the SSH tunnel subprocess for the given tunnel config."""
        self.stop()

        user = tunnel.get("ssh_user", "")
        host = tunnel["ssh_host"]
        port = str(tunnel.get("ssh_port", 22))
        auth_type = tunnel.get("auth_type", "key")
        compression = tunnel.get("ssh_compression", True)

        destination = f"{user}@{host}" if user else host

        ssh_args = [
            "-D", str(socks5_port),
            "-N",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={host_key.KNOWN_HOSTS_PATH}",
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
        ]
        if compression:
            ssh_args.append("-C")
        ssh_args.extend(["-p", port, destination])

        # sshpass via fd avoids leaking the password into `ps`.
        pass_fds = ()
        r_fd = None
        if auth_type == "password":
            r_fd, w_fd = os.pipe()
            try:
                os.write(w_fd, (password + "\n").encode())
            finally:
                os.close(w_fd)
            cmd = ["sshpass", "-d", str(r_fd), "ssh"] + ssh_args
            pass_fds = (r_fd,)
            display_cmd = ["sshpass", "-d", "***", "ssh"] + ssh_args
        else:
            key = tunnel.get("ssh_key", "")
            cmd = ["ssh", "-i", key] + ssh_args
            display_cmd = cmd

        self._current_name = tunnel.get("name", destination)

        ok = self._start_process(
            cmd, pass_fds=pass_fds, display_cmd=" ".join(display_cmd))

        if r_fd is not None:
            try:
                os.close(r_fd)
            except OSError:
                pass

        return ok

    def _probe_ready(self, port):
        """SOCKS5 handshake probe — sends method negotiation, expects success."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.settimeout(PORT_PROBE_TIMEOUT)
            s.connect(("127.0.0.1", port))
            s.sendall(b"\x05\x01\x00")
            if s.recv(2) != b"\x05\x00":
                raise OSError("listener is not a SOCKS5 proxy")
            return True
        except (ConnectionRefusedError, OSError):
            return False
        finally:
            s.close()


async def run_proxy(config: dict, stats: Stats, control: dict):
    """Start the HTTP→SOCKS5 proxy server (runs in background thread's asyncio loop)."""
    socks_addr = f"127.0.0.1:{config['socks5_port']}"
    listen_host = "127.0.0.1"
    listen_port = int(config["http_listen_port"])

    semaphore = asyncio.Semaphore(MAX_CLIENT_CONNECTIONS)

    async def limited_client(reader, writer):
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=1)
        except asyncio.TimeoutError:
            writer.write(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n\r\n")
            await writer.drain()
            await _close_writer(writer)
            return
        try:
            await handle_client(reader, writer, socks_addr, stats)
        finally:
            semaphore.release()

    server = await asyncio.start_server(
        limited_client, listen_host, listen_port, limit=MAX_HEADER_BYTES,
    )
    control["server"] = server
    logger.info("HTTP proxy listening on %s:%d → SOCKS5 %s", listen_host, listen_port, socks_addr)
    async with server:
        await server.serve_forever()


# ── Thread-owned asyncio runtime ────────────────────────────────────


class ProxyRuntime:
    """Thin adapter: AsyncRuntime + proxy-specific coroutine factory."""

    def __init__(self, stats):
        self._stats = stats
        self._rt = AsyncRuntime("MagicProxyHTTP", stop_timeout=5)

    @property
    def running(self):
        return self._rt.running

    @property
    def error(self):
        return self._rt.error

    def start(self, config):
        stats = self._stats
        cfg = dict(config)

        def factory(loop):
            control = {}
            task = loop.create_task(run_proxy(cfg, stats, control))

            def stop_fn():
                loop.call_soon_threadsafe(task.cancel)

            return task, stop_fn

        return self._rt.start(factory)

    def stop(self, timeout=5):
        return self._rt.stop(timeout)
