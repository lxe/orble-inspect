"""SSH to a box through Orble's control plane, as ``orble ssh`` does without IPv6.

The control plane's ``GET /v1/connect`` upgrades an HTTPS connection to a TCP stream to the box's sshd. It takes the
client's SSH greeting in a header (``SSH-Hello``) and hands it to sshd itself, so the greeting must not go down the
stream again. asyncssh writes its greeting first thing, so it talks to one end of a socket pair; a relay reads the
greeting off the other end, makes the upgrade request with it, and then copies bytes both ways.
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


class TunnelError(Exception):
    pass


async def _upgrade(api_url: str, token: str, host: str, hello: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    url = urlsplit(api_url)
    secure = url.scheme == "https"
    port = url.port or (443 if secure else 80)
    reader, writer = await asyncio.open_connection(
        url.hostname, port, ssl=ssl.create_default_context() if secure else None,
        server_hostname=url.hostname if secure else None,
    )
    query = urlencode({"host": host, "port": "22"})
    writer.write(
        (
            f"GET /v1/connect?{query} HTTP/1.1\r\nHost: {url.netloc}\r\nAuthorization: Bearer {token}\r\n"
            f"SSH-Hello: {hello}\r\nConnection: Upgrade\r\nUpgrade: sbx-tcp\r\n\r\n"
        ).encode()
    )
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0].decode(errors="replace")
    if " 101 " not in status + " ":
        body = b""
        with contextlib.suppress(Exception):
            body = await asyncio.wait_for(reader.read(4096), 5)
        writer.close()
        raise TunnelError(f"{host}: {status} {body.decode(errors='replace').strip()}")
    return reader, writer


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


async def _relay(local: socket.socket, api_url: str, token: str, host: str) -> None:
    reader, writer = await asyncio.open_connection(sock=local)
    try:
        hello = (await reader.readuntil(b"\n")).decode().rstrip("\r\n")
        remote_reader, remote_writer = await _upgrade(api_url, token, host, hello)
    except BaseException:
        writer.close()
        raise
    try:
        await asyncio.gather(_pipe(reader, remote_writer), _pipe(remote_reader, writer))
    finally:
        writer.close()
        remote_writer.close()


async def connect(
    api_url: str, token: str, host: str, host_key: str, client_key: asyncssh.SSHKey
) -> asyncssh.SSHClientConnection:
    """An SSH connection to ``host`` (``<box>.<account>.orble.tech``) as ``agent``.

    The box's host key comes from the API, so it's checked without a first-use prompt.
    """
    ours, theirs = socket.socketpair()
    relay = asyncio.ensure_future(_relay(theirs, api_url, token, host))
    known = asyncssh.import_known_hosts(f"{host} {host_key}\n")
    try:
        conn = await asyncssh.connect(
            sock=ours,
            host=host,
            username=USER,
            client_keys=[client_key],
            known_hosts=known,
            agent_path=None,
            config=[],
            keepalive_interval=30,
        )
    except BaseException:
        relay.cancel()
        ours.close()
        raise
    conn._orble_relay = relay  # type: ignore[attr-defined]  # kept alive with the connection
    return conn
