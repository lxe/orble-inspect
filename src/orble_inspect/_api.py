"""Orble's REST API: boxes, SSH keys and snapshots (https://orble.tech/docs)."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import httpx

DEFAULT_URL = "https://orble.tech"


class OrbleError(Exception):
    """An answer from Orble's API that isn't a success."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{status}: {message}" if status else message)
        self.status = status
        self.message = message


def api_url() -> str:
    return (os.environ.get("ORBLE_API_URL") or DEFAULT_URL).rstrip("/")


def api_key() -> str:
    key = os.environ.get("ORBLE_API_KEY")
    if not key:
        raise OrbleError(0, "set ORBLE_API_KEY to an Orble API key (orble.tech/settings?tab=api)")
    return key


class Api:
    """An async client for the calls this package makes."""

    def __init__(self, url: str | None = None, key: str | None = None) -> None:
        self.url = (url or api_url()).rstrip("/")
        self.key = key or api_key()
        self._http = httpx.AsyncClient(
            base_url=self.url,
            headers={"Authorization": f"Bearer {self.key}", "User-Agent": "orble-inspect"},
            timeout=httpx.Timeout(180, connect=30),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def call(self, method: str, path: str, body: Any = None, ok404: bool = False) -> Any:
        response = await self._http.request(method, path, json=body)
        if response.status_code == 404 and ok404:
            return None
        if response.status_code >= 300:
            try:
                message = response.json().get("error") or response.text
            except ValueError:
                message = response.text
            raise OrbleError(response.status_code, message.strip())
        return response.json() if response.content else None

    # boxes
    async def create_box(self, name: str, size: str | None, snapshot: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name}
        if size:
            body["size"] = size
        if snapshot:
            body["from"] = snapshot
        box: dict[str, Any] = await self.call("POST", "/v1/boxes", body)
        return box

    async def box(self, name: str) -> dict[str, Any] | None:
        box: dict[str, Any] | None = await self.call("GET", f"/v1/boxes/{name}", ok404=True)
        return box

    async def boxes(self) -> list[dict[str, Any]]:
        boxes: list[dict[str, Any]] = await self.call("GET", "/v1/boxes")
        return boxes

    async def delete_box(self, name: str) -> None:
        await self.call("DELETE", f"/v1/boxes/{name}", ok404=True)

    async def keep_awake(self, name: str) -> None:
        await self.call("PATCH", f"/v1/boxes/{name}", {"keep_awake": True})

    async def exec_then_delete(self, name: str, command: str) -> None:
        """Run ``command`` in the box and delete the box when it exits, whatever happens to this process."""
        await self.call("POST", f"/v1/boxes/{name}/exec", {"command": command, "then": "delete"})

    # SSH keys
    async def add_key(self, public_key: str, name: str) -> str:
        key: dict[str, Any] = await self.call("POST", "/v1/ssh-keys", {"public_key": public_key, "name": name})
        return str(key["fingerprint"])

    async def keys(self) -> list[dict[str, Any]]:
        keys: list[dict[str, Any]] = await self.call("GET", "/v1/ssh-keys")
        return keys

    async def remove_key(self, fingerprint: str) -> None:
        await self.call("DELETE", f"/v1/ssh-keys/{quote(fingerprint, safe='')}", ok404=True)

    # snapshots
    async def snapshots(self) -> list[dict[str, Any]]:
        snapshots: list[dict[str, Any]] = await self.call("GET", "/v1/snapshots")
        return snapshots
