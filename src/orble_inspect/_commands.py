"""The shell lines a box runs for each sandbox operation. No I/O here, so the unit tests check them directly.

Every operation is one ``/bin/sh`` line run over SSH as the box's user, ``agent``, who has passwordless sudo.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

SH = "/bin/sh"
SUDO = "/usr/bin/sudo"
TIMEOUT = "/usr/bin/timeout"
ENV = "/usr/bin/env"
SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

KILL_AFTER = 5
"""Seconds between the TERM and the KILL a timed-out command gets."""

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# cd to $0, then run "$@": neither the directory nor the arguments go through word splitting.
_CD_AND_RUN = 'cd -- "$0" && exec "$@"'


@dataclass(frozen=True)
class Compose:
    """A compose project in the box, and one of its services."""

    project: str
    directory: str
    file: str
    service: str

    def base(self) -> list[str]:
        return ["docker", "compose", "--project-name", self.project, "--project-directory", self.directory,
                "--file", self.file]


def check_env(env: dict[str, str] | None) -> None:
    for name in env or {}:
        if not _ENV_NAME.fullmatch(name):
            raise ValueError(f"{name!r} isn't an environment variable name")


def sudo_user(user: str) -> str:
    """``sudo -u`` takes a uid as ``#<uid>``."""
    return f"#{user}" if user.isdigit() else user


def exec_argv(
    cmd: list[str],
    *,
    cwd: str,
    env: dict[str, str] | None,
    user: str | None,
    timeout: int | None,
    compose: Compose | None,
) -> list[str]:
    """The argv that runs ``cmd`` as Inspect asks.

    In the box: ``timeout`` (which signals the command's whole process group), then ``sudo`` for another user, then a
    shell that changes to ``cwd`` as that user and ``exec``s ``cmd``; ``env`` goes through ``env`` so it survives
    ``sudo``. In a compose service: ``docker compose exec`` with the same ``timeout`` inside the container.
    """
    if not cmd:
        raise ValueError("cmd must not be empty")
    check_env(env)
    limit = [TIMEOUT, "-k", f"{KILL_AFTER}s", f"{timeout}s"] if timeout is not None else []
    if compose is not None:
        argv = [*compose.base(), "exec", "-T", "--workdir", cwd]
        if user is not None:
            argv += ["--user", user]
        for name, value in (env or {}).items():
            argv += ["--env", f"{name}={value}"]
        return [*argv, compose.service, *limit, *cmd]
    run = [SH, "-c", _CD_AND_RUN, cwd]
    if env:
        run += [ENV, *(f"{name}={value}" for name, value in env.items())]
    run += cmd
    if user is not None:
        return [*limit, SUDO, "-n", "-H", "-u", sudo_user(user), "--", *run]
    return [*limit, *run]


def script_argv(script: str, *args: str, user: str | None, compose: Compose | None) -> list[str]:
    """A POSIX ``script`` (``$1``... are ``args``) run as ``user`` with the system PATH.

    For the file operations: the programs they use never come from a directory the sandbox's user can write to.
    """
    inner = [SH, "-c", f"PATH={SYSTEM_PATH}\nexport PATH\nunset CDPATH\n{script}", "sh", *args]
    if compose is not None:
        argv = [*compose.base(), "exec", "-T"]
        if user is not None:
            argv += ["--user", user]
        return [*argv, compose.service, *inner]
    if user is not None:
        return [SUDO, "-n", "-H", "-u", sudo_user(user), "--", *inner]
    return inner


def line(argv: list[str], *, stdin: str | None = None, remove: list[str] | None = None) -> str:
    """One shell line: ``argv``, with stdin from a file if given, then the temp files removed. Keeps argv's status."""
    out = shlex.join(argv)
    if stdin is not None:
        out += f" < {shlex.quote(stdin)}"
    if remove:
        out += f"; rc=$?; /bin/rm -f -- {shlex.join(remove)}; exit $rc"
    return out


READ = """\
if [ -d "$1" ]; then echo "$1: Is a directory" >&2; exit 21; fi
if [ -e "$1" ] && [ -r "$1" ]; then
  size=$(stat -L -c %s -- "$1") || exit 1
  if [ "$size" -gt "$2" ]; then echo "$1: larger than $2 bytes" >&2; exit 27; fi
fi
exec cat -- "$1"
"""
"""Print a file of at most ``$2`` bytes. A directory exits 21, a larger file 27, before anything is sent."""

WRITE = """\
parent=$(dirname -- "$1") || exit 1
mkdir -p -- "$parent" || exit 1
if [ -d "$1" ]; then echo "$1: Is a directory" >&2; exit 21; fi
exec cat > "$1"
"""
"""Write stdin to a file, making its directory first. Through the file, so its owner, mode and permissions apply."""

PWD = "exec pwd"
