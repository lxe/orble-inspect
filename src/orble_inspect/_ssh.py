"""SSH to a box through Orble's API, as ``orble ssh`` does without IPv6.

The API's ``GET /v1/connect`` upgrades an HTTPS connection to a TCP stream to the box's sshd. It takes the client's
SSH greeting in a header (``SSH-Hello``) and hands it to sshd itself, so the greeting must not go down the stream
again. asyncssh's greeting is fixed by ``client_version``, so the upgrade happens first, with that greeting, and its
errors (a bad key, no such box) are raised here. asyncssh then talks to one end of a socket pair; a relay drops its
greeting line and copies bytes both ways.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import ssl
from urllib.parse import urlencode, urlsplit

import asyncssh

USER = "agent"
"""Orble's box user: passwordless sudo, Docker, Python and Node."""
CLIENT_VERSION = "OrbleInspect"
GREETING = f"SSH-2.0-{CLIENT_VERSION}"
TIMEOUT = 30
"""Seconds for the HTTPS connection, the upgrade, and the SSH handshake, each."""


class TunnelError(Exception):
    """The API refused the upgrade: ``status`` is its HTTP status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


async def _upgrade(api_url: str, token: str, host: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """The upgraded stream to the box's sshd."""
    url = urlsplit(api_url)
    secure = url.scheme == "https"
    port = url.port or (443 if secure else 80)
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(
            url.hostname,
            port,
            ssl=ssl.create_default_context() if secure else None,
            server_hostname=url.hostname if secure else None,
        ),
        TIMEOUT,
    )
    try:
        query = urlencode({"host": host, "port": "22"})
        writer.write(
            (
                f"GET /v1/connect?{query} HTTP/1.1\r\nHost: {url.netloc}\r\nAuthorization: Bearer {token}\r\n"
                f"SSH-Hello: {GREETING}\r\nConnection: Upgrade\r\nUpgrade: sbx-tcp\r\n\r\n"
            ).encode()
        )
        await writer.drain()
        # Bytes after the headers stay in the reader's buffer, so the relay passes them on.
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), TIMEOUT)
        status_line = head.split(b"\r\n", 1)[0].decode(errors="replace")
        parts = status_line.split(" ", 2)
        status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        if status != 101:
            body = b""
            with contextlib.suppress(Exception):
                body = await asyncio.wait_for(reader.read(4096), 5)
            raise TunnelError(status, f"{host}: {status_line} {body.decode(errors='replace').strip()}")
        return reader, writer
    except BaseException:
        writer.close()
        raise


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(256 * 1024):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        with contextlib.suppress(Exception):
            if writer.can_write_eof():
                writer.write_eof()


async def _relay(
    local: socket.socket, remote_reader: asyncio.StreamReader, remote_writer: asyncio.StreamWriter
) -> None:
    try:
        reader, writer = await asyncio.open_connection(sock=local)
    except BaseException:
        local.close()
        remote_writer.close()
        raise
    try:
        greeting = await reader.readuntil(b"\n")  # sshd already has it, from the header
        if greeting.rstrip(b"\r\n").decode(errors="replace") == GREETING:
            await asyncio.gather(_pipe(reader, remote_writer), _pipe(remote_reader, writer))
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, OSError):
        pass
    finally:
        writer.close()
        remote_writer.close()


async def connect(
    api_url: str, token: str, host: str, host_key: str, client_key: asyncssh.SSHKey
) -> asyncssh.SSHClientConnection:
    """An SSH connection to ``host`` (``<box>.<account>.orble.tech``) as ``agent``.

    The box's host key comes from the API, so it's checked without a first-use prompt. Raises ``TunnelError`` when the
    API refuses the connection.
    """
    known = asyncssh.import_known_hosts(f"{host} {host_key}\n")
    remote_reader, remote_writer = await _upgrade(api_url, token, host)
    ours, theirs = socket.socketpair()
    relay = asyncio.ensure_future(_relay(theirs, remote_reader, remote_writer))
    try:
        conn = await asyncio.wait_for(
            asyncssh.connect(
                sock=ours,
                host=host,
                username=USER,
                client_keys=[client_key],
                known_hosts=known,
                client_version=CLIENT_VERSION,
                agent_path=None,
                config=[],
                keepalive_interval=30,
            ),
            TIMEOUT,
        )
    except BaseException:
        relay.cancel()
        ours.close()
        theirs.close()
        remote_writer.close()
        raise
    conn._orble_relay = relay  # type: ignore[attr-defined]  # kept alive with the connection
    return conn
