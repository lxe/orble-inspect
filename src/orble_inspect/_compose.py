"""A compose project to run with Docker inside the box.

The compose file (or a ``Dockerfile``, wrapped in the compose file Inspect's Docker sandbox makes for one) goes to
the box with its directory, so build contexts next to it work as they do locally.
"""

from __future__ import annotations

import io
import os
import tarfile
from dataclasses import dataclass
from pathlib import Path

import yaml
from inspect_ai.util import ComposeConfig, is_dockerfile, parse_compose_yaml

GENERATED = ".orble-inspect-compose.yaml"
SKIPPED = {".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache", ".mypy_cache"}
"""Directories never sent to the box: version control, virtualenvs and caches. A ``logs`` directory next to the compose
file is left out too: Inspect writes its logs there as the eval runs, which would make every sample pack the project
again."""


def _skipped(name: str) -> bool:
    """A path part that stays on this machine: a skipped directory, or a ``.env`` file (Inspect reads API keys from it,
    and an untrusted model shouldn't find them in its box)."""
    return name in SKIPPED or name == ".env" or name.startswith(".env.")


MAX_BYTES = 512 * 1024 * 1024

DOCKERFILE_COMPOSE = """\
services:
  default:
    build:
      context: "."
      dockerfile: "{dockerfile}"
    command: "tail -f /dev/null"
    init: true
    network_mode: none
    stop_grace_period: 1s
"""
"""What Inspect's Docker sandbox runs for a lone ``Dockerfile``."""


@dataclass(frozen=True)
class Source:
    """A packed project directory and the compose file in it."""

    archive: bytes
    file: str
    text: str
    """The compose file, to see which ``SAMPLE_METADATA_*`` variables it uses."""
    services: list[str]
    default_service: str


_cache: dict[tuple[str, float], Source] = {}


def compose_source(compose: str | ComposeConfig) -> Source:
    """Pack the project once per version of its files; samples share the result."""
    if isinstance(compose, ComposeConfig):
        return _from_config(compose)
    path = Path(compose).resolve()
    key = (str(path), _newest(path.parent))
    if key not in _cache:
        _cache[key] = _from_file(path)
    return _cache[key]


def default_service(config: ComposeConfig) -> str:
    """The service Inspect calls ``default``: the one with ``x-default: true``, else the one named so."""
    for name, service in config.services.items():
        extra = service.model_extra or {}
        if getattr(service, "x_default", None) is True or extra.get("x-default") is True:
            return name
    if "default" in config.services:
        return "default"
    raise ValueError("No 'default' service in the compose file: name one 'default' or give one 'x-default: true'.")


def _from_file(path: Path) -> Source:
    if not path.is_file():
        raise FileNotFoundError(f"{path} doesn't exist")
    extra: dict[str, bytes] = {}
    if is_dockerfile(str(path)):
        text = DOCKERFILE_COMPOSE.format(dockerfile=path.name)
        extra[GENERATED] = text.encode()
        config = ComposeConfig.model_validate(yaml.safe_load(text))
        file = GENERATED
    else:
        text = path.read_text()
        config = parse_compose_yaml(str(path), multiple_services=True)
        file = path.name
    return Source(_pack(path.parent, extra), file, text, list(config.services), default_service(config))


def _from_config(config: ComposeConfig) -> Source:
    text = yaml.safe_dump(config.model_dump(mode="json", by_alias=True, exclude_none=True), sort_keys=False)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        _add(tar, GENERATED, text.encode())
    # Inspect resolves a ComposeConfig before it gets here, so it takes no metadata variables.
    return Source(buffer.getvalue(), GENERATED, "", list(config.services), default_service(config))


def _pack(directory: Path, extra: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        tar.add(directory, arcname=".", filter=_owned)
        for name, data in extra.items():
            _add(tar, name, data)
    data = buffer.getvalue()
    if len(data) > MAX_BYTES:
        raise ValueError(
            f"{directory} packs to {len(data) >> 20} MiB, more than the {MAX_BYTES >> 20} MiB sent to each box. "
            "Move the compose file to a smaller directory, or start from a snapshot."
        )
    return data


def _owned(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    """Every entry owned by the box's user; no devices, and nothing ``_skipped``."""
    parts = [p for p in info.name.split("/") if p not in ("", ".")]
    if info.isdev() or any(_skipped(part) for part in parts) or parts[:1] == ["logs"]:
        return None
    info.uid = info.gid = 1000
    info.uname = info.gname = "agent"
    return info


def _add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o644
    tar.addfile(info, io.BytesIO(data))


def _newest(directory: Path) -> float:
    """The newest change in a directory tree, so an edited build context is packed again."""
    newest = directory.stat().st_mtime
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if not _skipped(d) and not (root == str(directory) and d == "logs")]
        for name in files:
            if _skipped(name):
                continue
            try:
                newest = max(newest, os.stat(os.path.join(root, name)).st_mtime)
            except OSError:
                continue
    return newest
