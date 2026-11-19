"""How each sample's Orble box is made."""

from __future__ import annotations

import json
import re
from pathlib import Path

from inspect_ai.util import ComposeConfig, is_compose_yaml, is_dockerfile
from pydantic import BaseModel, ConfigDict

SIZES = ("s", "m", "l")
_PREFIX = re.compile(r"[a-z][a-z0-9-]{0,11}")


class OrbleSandboxEnvironmentConfig(BaseModel):
    """How each sample's box is made. The API key is never part of it (it would end up in the eval log): set
    ``ORBLE_API_KEY``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    size: str | None = None
    """``s`` (2 vCPU, 4 GiB), ``m`` (4, 8) or ``l`` (8, 16). Default: ``s``, or the snapshot's size."""

    snapshot: str | None = None
    """Start each sample from this snapshot: one built from a Dockerfile with ``orble snapshot build``, or a box
    saved with ``orble snapshot``. A box from a snapshot is up in about a second, programs and all."""

    compose: str | None = None
    """A ``compose.yaml`` or ``Dockerfile`` to run with Docker inside the box. Without it, commands run in the box
    itself (Debian 13, user ``agent`` with passwordless sudo, Docker, Python and Node)."""

    working_dir: str | None = None
    """Where commands run and relative paths resolve. Default: ``/home/agent``, or the container's working
    directory with ``compose``."""

    name_prefix: str = "inspect"
    """Box names are this, then a random suffix."""

    delete_after: int | None = 6 * 3600
    """Seconds after which a box deletes itself, whatever it's doing: a backstop for boxes an eval that was
    killed never cleaned up. Keep it above your longest sample; ``None`` turns it off."""

    compose_timeout: float = 900
    """Seconds ``docker compose up`` may take, pulls and builds included."""

    def __hash__(self) -> int:
        return hash(self.model_dump_json())

    def check(self) -> None:
        """Raise ``ValueError`` for a value Orble would refuse, before any request."""
        if self.size is not None and self.size not in SIZES:
            raise ValueError(f"size is s, m or l, not {self.size!r}")
        if not _PREFIX.fullmatch(self.name_prefix):
            raise ValueError("name_prefix is 1 to 12 lowercase letters, digits or '-', starting with a letter")
        if self.working_dir is not None and not self.working_dir.startswith("/"):
            raise ValueError("working_dir is an absolute path")
        if self.delete_after is not None and self.delete_after < 60:
            raise ValueError("delete_after is at least 60 seconds")
        if self.compose is not None and not (is_compose_yaml(self.compose) or is_dockerfile(self.compose)):
            raise ValueError(f"compose is a compose.yaml or a Dockerfile, not {self.compose!r}")


Resolved = tuple[OrbleSandboxEnvironmentConfig, "str | ComposeConfig | None"]


def resolve_config(config: object) -> Resolved:
    """What Inspect passes as ``config``, as our settings and a compose source.

    Inspect passes nothing, our object, a ``ComposeConfig``, or a path: a ``compose.yaml`` or ``Dockerfile`` (run
    with Docker in the box) or a JSON file of our settings.
    """
    compose: str | ComposeConfig | None = None
    if config is None:
        resolved = OrbleSandboxEnvironmentConfig()
    elif isinstance(config, OrbleSandboxEnvironmentConfig):
        resolved = config
    elif isinstance(config, ComposeConfig) or (
        isinstance(config, str) and (is_compose_yaml(config) or is_dockerfile(config))
    ):
        resolved = OrbleSandboxEnvironmentConfig()
        compose = config
    elif isinstance(config, str) and config.endswith(".json"):
        resolved = OrbleSandboxEnvironmentConfig.model_validate(json.loads(Path(config).read_text()))
    else:
        raise TypeError(
            "The orble sandbox takes an OrbleSandboxEnvironmentConfig, a ComposeConfig, or the path of a "
            f"compose.yaml, a Dockerfile or a JSON file, not {config!r}"
        )
    resolved.check()
    if compose is None and resolved.compose is not None:
        compose = resolved.compose
    return resolved, compose
