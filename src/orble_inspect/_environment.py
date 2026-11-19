"""The ``orble`` sandbox environment: one Orble box per sample."""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import errno
import logging
import os
import posixpath
import shlex
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any, Literal, overload

import anyio
import asyncssh
import httpx
from inspect_ai.util import (
    ComposeConfig,
    ExecResult,
    OutputLimitExceededError,
    SandboxConnection,
    SandboxEnvironment,
    SandboxEnvironmentConfigType,
    SandboxEnvironmentLimits,
    SandboxUnavailableError,
    SandboxUserUnsupportedError,
    sandboxenv,
    trace_message,
)
from pydantic import BaseModel
from typing_extensions import override

from . import _commands as cmds
from ._api import Api, OrbleError, api_key, api_url
from ._compose import compose_source
from ._config import OrbleSandboxEnvironmentConfig, resolve_config
from ._ssh import connect

logger = logging.getLogger(__name__)

HOME = "/home/agent"
TEMP = f"{HOME}/.orble-inspect"
"""Scripts and archives on their way in: owned by ``agent``, mode 0700, so no other user can read or swap them."""
COMPOSE_DIR = f"{HOME}/inspect-compose"
COMPOSE_PROJECT = "inspect"
FILE_TIMEOUT = 600
MAX_LINE = 64 * 1024
"""Longer command lines go to the box as a script file."""
SLACK = 15
"""Seconds an exec may run beyond its own timeout and the KILL grace before the connection gives up on it."""
CLEANUP_PREFIX_ENV = "ORBLE_INSPECT_PREFIX"

_now = time.monotonic


class _Run:
    """What one eval made: an id, its SSH key on the account, and the boxes it hasn't deleted yet."""

    def __init__(self) -> None:
        self.id = uuid.uuid4().hex[:8]
        self.boxes: set[str] = set()
        self.key: asyncssh.SSHKey | None = None
        self.key_fingerprint: str | None = None
        self.key_lock = asyncio.Lock()
        self.tasks = 0


_unscoped: ContextVar[_Run | None] = ContextVar("orble_run", default=None)


def _run() -> _Run:
    """The running eval's state, on the state Inspect keeps for the eval when there is one."""
    try:
        from inspect_ai.util._sandbox.lifecycle import sandbox_lifecycle_state

        scope = sandbox_lifecycle_state()
    except ImportError:
        scope = None
    if scope is not None:
        run: _Run = scope.get(_Run)
        return run
    run_ = _unscoped.get()
    if run_ is None:
        run_ = _Run()
        _unscoped.set(run_)
    return run_


# One API client per event loop: an httpx client can't outlive the loop it was made on.
_apis: dict[int, Api] = {}


def _api() -> Api:
    loop = id(asyncio.get_running_loop())
    if loop not in _apis:
        _apis[loop] = Api()
    return _apis[loop]


# SSH keys this process added to the account, removed at exit if an eval didn't get to it.
_keys_to_remove: set[str] = set()


@atexit.register
def _remove_keys_at_exit() -> None:
    for fingerprint in list(_keys_to_remove):
        with contextlib.suppress(Exception):
            from urllib.parse import quote

            httpx.delete(
                f"{api_url()}/v1/ssh-keys/{quote(fingerprint, safe='')}",
                headers={"Authorization": f"Bearer {api_key()}"},
                timeout=10,
            )


async def _key(run: _Run) -> asyncssh.SSHKey:
    """The eval's SSH key, made and added to the account on first use. Boxes made after get it."""
    async with run.key_lock:
        if run.key is None:
            key = asyncssh.generate_private_key("ssh-ed25519", comment=f"orble-inspect-{run.id}")
            public = key.export_public_key().decode().strip()
            run.key_fingerprint = await _api().add_key(public, f"orble-inspect-{run.id}")
            _keys_to_remove.add(run.key_fingerprint)
            run.key = key
        return run.key


async def _remove_key(run: _Run) -> None:
    if run.key_fingerprint is None:
        return
    with contextlib.suppress(Exception):
        await _api().remove_key(run.key_fingerprint)
        _keys_to_remove.discard(run.key_fingerprint)
    run.key, run.key_fingerprint = None, None


class _Output:
    def __init__(self) -> None:
        self.exit_code = -1
        self.over_limit = False
        self.stdout = bytearray()
        self.stderr = bytearray()


class _Box:
    """A box and its SSH connection, shared by the compose services in it."""

    def __init__(self, name: str, hostname: str, conn: asyncssh.SSHClientConnection) -> None:
        self.name = name
        self.hostname = hostname
        self.conn = conn

    async def run(self, line: str, *, stdin: bytes | None, timeout: float | None, limit: int) -> _Output:
        """Run a shell line; ``stdin`` is streamed in, output streams back up to ``limit`` bytes per stream."""
        if len(line) > MAX_LINE:
            script = f"{TEMP}/{uuid.uuid4().hex}.sh"
            wrote = await self.run(f"/bin/cat > {script}", stdin=line.encode(), timeout=FILE_TIMEOUT, limit=4096)
            if wrote.exit_code != 0:
                raise RuntimeError(f"Couldn't send a long command to box {self.name}: {bytes(wrote.stderr)!r}")
            line = f"/bin/sh {script}; rc=$?; /bin/rm -f -- {script}; exit $rc"
        out = _Output()
        try:
            process = await self.conn.create_process(line, encoding=None)
        except (asyncssh.Error, OSError) as error:
            raise SandboxUnavailableError(f"Orble box {self.name} isn't reachable: {error}") from error

        async def feed() -> None:
            try:
                if stdin:
                    view = memoryview(stdin)
                    for start in range(0, len(view), 1 << 20):
                        process.stdin.write(bytes(view[start : start + (1 << 20)]))
                        await process.stdin.drain()
                process.stdin.write_eof()
            except (asyncssh.Error, BrokenPipeError, ConnectionError):
                pass

        async def drain(stream: Any, buffer: bytearray) -> None:
            while chunk := await stream.read(1 << 16):
                buffer += chunk
                if len(buffer) > limit:
                    out.over_limit = True
                    process.close()
                    return

        try:
            with anyio.fail_after(timeout):
                await asyncio.gather(feed(), drain(process.stdout, out.stdout), drain(process.stderr, out.stderr))
                if not out.over_limit:
                    await process.wait_closed()
        except TimeoutError:
            process.close()
            raise
        if not out.over_limit:
            if process.exit_status is not None and process.exit_status >= 0:
                out.exit_code = process.exit_status
            elif process.exit_signal is not None:
                out.exit_code = 128 + _signal_number(process.exit_signal[0])
        return out

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            self.conn.close()
            await asyncio.wait_for(self.conn.wait_closed(), 5)
        relay = getattr(self.conn, "_orble_relay", None)
        if relay is not None:
            relay.cancel()


@sandboxenv(name="orble")
class OrbleSandboxEnvironment(SandboxEnvironment):
    """A sample's sandbox: an Orble box, deleted when the sample ends.

    Commands run in the box (Debian 13, user ``agent`` with passwordless sudo) or, with a compose file or
    ``Dockerfile``, in a service Docker runs inside the box. Several services in one compose file share the box.
    """

    def __init__(self, box: _Box, working_dir: str, compose: cmds.Compose | None = None) -> None:
        super().__init__()
        self._box = box
        self._working_dir = working_dir
        self._compose = compose

    @property
    def box_name(self) -> str:
        """The Orble box's name."""
        return self._box.name

    # --- the sandbox ---

    @override
    async def exec(
        self,
        cmd: list[str],
        input: str | bytes | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        user: str | None = None,
        timeout: int | None = None,
        timeout_retry: bool = True,
        concurrency: bool = True,
    ) -> ExecResult[str]:
        _check_user(user)
        argv = cmds.exec_argv(
            cmd,
            cwd=self._path(cwd) if cwd is not None else self._working_dir,
            env=env,
            user=user,
            timeout=timeout,
            compose=self._compose,
        )
        stdin = input.encode() if isinstance(input, str) else input
        started = _now()
        limit = SandboxEnvironmentLimits.MAX_EXEC_OUTPUT_SIZE
        try:
            result = await self._box.run(
                shlex.join(argv),
                stdin=stdin,
                timeout=None if timeout is None else timeout + cmds.KILL_AFTER + SLACK,
                limit=limit,
            )
        except TimeoutError as error:
            raise TimeoutError(f"Command timed out after {timeout} seconds") from error
        elapsed = _now() - started
        stdout, stderr = _text(result.stdout), _text(result.stderr)
        # `timeout` ends a command with 124 (TERM) or 137 (KILL); a command can exit so by itself, but not early.
        if timeout is not None and result.exit_code in (124, 137, 143) and elapsed >= timeout:
            timed_out = TimeoutError(f"Command timed out after {timeout} seconds")
            if stdout or stderr:
                timed_out.truncated_output = stdout + stderr  # type: ignore[attr-defined]
            raise timed_out
        if result.exit_code == 126 and "permission denied" in (stderr + stdout).casefold():
            raise PermissionError(errno.EACCES, f"Permission denied: {(stderr or stdout).strip()}", cmd[0])
        if result.over_limit:
            raise OutputLimitExceededError(
                SandboxEnvironmentLimits.MAX_EXEC_OUTPUT_SIZE_STR, _text(bytes(result.stdout[-4096:]))
            )
        return ExecResult(success=result.exit_code == 0, returncode=result.exit_code, stdout=stdout, stderr=stderr)

    @override
    async def write_file(self, file: str, contents: str | bytes, *, user: str | None = None) -> None:
        _check_user(user)
        data = contents.encode() if isinstance(contents, str) else contents
        argv = cmds.script_argv(cmds.WRITE, self._path(file), user=user, compose=self._compose)
        result = await self._box.run(
            shlex.join(argv), stdin=data, timeout=FILE_TIMEOUT, limit=SandboxEnvironmentLimits.MAX_EXEC_OUTPUT_SIZE
        )
        if result.exit_code != 0:
            _raise_file_error(_text(result.stderr), result.exit_code, file, user)

    @overload
    async def read_file(self, file: str, text: Literal[True] = True, *, user: str | None = None) -> str: ...

    @overload
    async def read_file(self, file: str, text: Literal[False], *, user: str | None = None) -> bytes: ...

    @override
    async def read_file(self, file: str, text: bool = True, *, user: str | None = None) -> str | bytes:
        _check_user(user)
        limit = SandboxEnvironmentLimits.MAX_READ_FILE_SIZE
        argv = cmds.script_argv(cmds.READ, self._path(file), str(limit), user=user, compose=self._compose)
        result = await self._box.run(shlex.join(argv), stdin=None, timeout=FILE_TIMEOUT, limit=limit)
        if result.exit_code == 27 or result.over_limit:
            raise OutputLimitExceededError(SandboxEnvironmentLimits.MAX_READ_FILE_SIZE_STR, None)
        if result.exit_code != 0:
            _raise_file_error(_text(result.stderr), result.exit_code, file, user)
        data = bytes(result.stdout)
        return data.decode("utf-8") if text else data

    @override
    async def connection(self, *, user: str | None = None) -> SandboxConnection:
        """``orble ssh <box>`` opens a shell in the box; ``container`` names the compose service."""
        return SandboxConnection(
            type="orble",
            command=f"orble ssh {self._box.name}",
            container=self._compose.service if self._compose is not None else None,
        )

    def _path(self, path: str) -> str:
        return posixpath.join(self._working_dir, path)

    # --- the lifecycle ---

    @override
    @classmethod
    def config_files(cls) -> list[str]:
        return ["compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml", "Dockerfile"]

    @override
    @classmethod
    def is_docker_compatible(cls) -> bool:
        return True

    @override
    @classmethod
    def config_deserialize(cls, config: dict[str, Any]) -> BaseModel:
        return OrbleSandboxEnvironmentConfig.model_validate(config)

    @override
    @classmethod
    async def task_init(cls, task_name: str, config: SandboxEnvironmentConfigType | None) -> None:
        """Check the configuration and the API key, and that a snapshot exists, before the first sample."""
        resolved, compose = resolve_config(config)
        if compose is not None:
            await anyio.to_thread.run_sync(compose_source, compose)
        run = _run()
        run.tasks += 1
        api = _api()
        await api.call("GET", "/v1/me")
        if resolved.snapshot:
            names = {s.get("name") for s in await api.snapshots()} | {s.get("alias") for s in await api.snapshots()}
            if resolved.snapshot not in names:
                raise ValueError(f"No snapshot {resolved.snapshot!r} in this Orble account (orble snapshots)")

    @override
    @classmethod
    async def sample_init(
        cls, task_name: str, config: SandboxEnvironmentConfigType | None, metadata: dict[str, str]
    ) -> dict[str, SandboxEnvironment]:
        resolved, compose = resolve_config(config)
        run = _run()
        api = _api()
        key = await _key(run)
        name = f"{resolved.name_prefix}-{run.id}{uuid.uuid4().hex[:6]}"
        with anyio.CancelScope(shield=True):  # a box made while the sample is cancelled is still recorded
            info = await api.create_box(name, resolved.size, resolved.snapshot)
            run.boxes.add(name)
        trace_message(logger, "orble", f"Created box {name} ({info.get('name')})")
        box: _Box | None = None
        try:
            with contextlib.suppress(OrbleError):  # older servers have no keep-awake; an SSH session keeps it up
                await api.keep_awake(name)
            if resolved.delete_after:
                _backstop(api, name, resolved.delete_after)
            box = await _connect(api, name, info, key)
            await _prepare(box, resolved, on_box=compose is None)
            if compose is None:
                return {"default": OrbleSandboxEnvironment(box, resolved.working_dir or HOME)}
            return await _compose_environments(box, resolved, compose)
        except BaseException:
            if box is not None:
                await box.close()
            await _delete(name)
            raise

    @override
    @classmethod
    async def sample_cleanup(
        cls,
        task_name: str,
        config: SandboxEnvironmentConfigType | None,
        environments: dict[str, SandboxEnvironment],
        interrupted: bool,
    ) -> None:
        done: set[str] = set()
        for environment in environments.values():
            if not isinstance(environment, OrbleSandboxEnvironment) or environment._box.name in done:
                continue
            done.add(environment._box.name)
            await environment._box.close()
            await _delete(environment._box.name)

    @override
    @classmethod
    async def task_cleanup(cls, task_name: str, config: SandboxEnvironmentConfigType | None, cleanup: bool) -> None:
        """Delete the boxes whose samples didn't clean up (by name, never by search), then the eval's SSH key."""
        run = _run()
        run.tasks = max(run.tasks - 1, 0)
        if run.boxes and not cleanup:
            names = ", ".join(sorted(run.boxes))
            print(f"Orble boxes left running for inspection: {names}", file=sys.stderr)
            print("Delete them with: inspect sandbox cleanup orble <box>", file=sys.stderr)
            run.boxes.clear()
        for name in list(run.boxes):
            await _delete(name)
        if run.tasks == 0:
            await _remove_key(run)

    @override
    @classmethod
    async def cli_cleanup(cls, id: str | None) -> None:
        """``inspect sandbox cleanup orble [box]``: one box, or every box named ``inspect-...``."""
        api = Api()
        try:
            if id is not None:
                if await api.box(id) is None:
                    print(f"No Orble box {id}.")
                    sys.exit(1)
                await api.delete_box(id)
                print(f"Deleted Orble box {id}")
                return
            prefix = (os.environ.get(CLEANUP_PREFIX_ENV) or "inspect") + "-"
            boxes = [b for b in await api.boxes() if (b.get("alias") or b.get("name") or "").startswith(prefix)]
            if not boxes:
                print(f"No Orble boxes named {prefix}...")
                # With no eval's boxes left, no eval is running: the SSH keys evals added are leftovers of killed ones.
                for key in await api.keys():
                    if str(key.get("name", "")).startswith("orble-inspect-"):
                        await api.remove_key(key["fingerprint"])
                        print(f"Removed SSH key {key['name']}")
                return
            for b in boxes:
                print(f"{b.get('alias')}  {b.get('name')}  {b.get('state')}")
            interactive = sys.stdin.isatty() and "CI" not in os.environ
            if interactive and input(f"Delete {len(boxes)} box(es)? [y/N] ").strip().lower() not in ("y", "yes"):
                print("Cancelled.")
                return
            for b in boxes:
                await api.delete_box(b.get("alias") or b["name"])
            print(f"Deleted {len(boxes)} box(es).")
        finally:
            await api.aclose()


def _backstop(api: Api, name: str, seconds: int) -> None:
    """A command in the box that deletes it after ``seconds``, whatever happens to this process."""

    async def go() -> None:
        with contextlib.suppress(Exception):
            await api.exec_then_delete(name, f"exec sleep {int(seconds)}")

    task = asyncio.ensure_future(go())
    _backstops.add(task)
    task.add_done_callback(_backstops.discard)


_backstops: set[asyncio.Future[None]] = set()


async def _connect(api: Api, name: str, info: dict[str, Any], key: asyncssh.SSHKey) -> _Box:
    hostname, host_key = info.get("hostname"), info.get("host_key")
    deadline = _now() + 120
    last: Exception | None = None
    while _now() < deadline:
        if not host_key or not hostname:
            info = await api.box(name) or {}
            hostname, host_key = info.get("hostname"), info.get("host_key")
            if not host_key or not hostname:
                await asyncio.sleep(1)
                continue
        try:
            conn = await connect(api.url, api.key, hostname, host_key, key)
            return _Box(name, hostname, conn)
        except (OSError, asyncssh.Error, TimeoutError, asyncio.IncompleteReadError) as error:
            last = error  # sshd still starting, or the key not yet in the box
            await asyncio.sleep(1)
    raise SandboxUnavailableError(f"Couldn't reach Orble box {name} over SSH: {last}")


async def _prepare(box: _Box, config: OrbleSandboxEnvironmentConfig, on_box: bool) -> None:
    """The private temp directory, and in the box the working directory, reachable by other users."""
    setup = f"/usr/bin/install -d -m 700 {TEMP}"
    working_dir = config.working_dir or HOME
    if on_box:
        # Orble's image answers any unknown user name as agent (nsswitch "anyuser"), for SSH logins under any name.
        # An eval logs in as agent and expects real users: adduser, sudo -u and getent must see only real accounts.
        setup += " && /usr/bin/sudo -n /bin/sed -i -E 's/[[:space:]]+anyuser//' /etc/nsswitch.conf"
        setup += f" && /bin/chmod o+x {HOME}"
        if working_dir != HOME:
            q = shlex.quote(working_dir)
            setup += f" && /usr/bin/sudo -n /bin/mkdir -p -- {q} && /usr/bin/sudo -n /bin/chown agent:agent -- {q}"
    result = await box.run(setup, stdin=None, timeout=120, limit=1 << 16)
    if result.exit_code != 0:
        raise RuntimeError(f"Couldn't prepare Orble box {box.name}: {_text(result.stderr).strip()}")


async def _compose_environments(
    box: _Box, config: OrbleSandboxEnvironmentConfig, compose: str | ComposeConfig
) -> dict[str, SandboxEnvironment]:
    """Unpack the compose project in the box, start it with Docker, and wrap each running service."""
    source = await anyio.to_thread.run_sync(compose_source, compose)
    project = cmds.Compose(COMPOSE_PROJECT, COMPOSE_DIR, f"{COMPOSE_DIR}/{source.file}", "")
    base = shlex.join(project.base())
    unpack = await box.run(
        f"/bin/mkdir -p {COMPOSE_DIR} && /bin/tar -xzf - -C {COMPOSE_DIR}",
        stdin=source.archive,
        timeout=600,
        limit=1 << 20,
    )
    if unpack.exit_code != 0:
        raise RuntimeError(f"Couldn't unpack the compose project in box {box.name}: {_text(unpack.stderr)}")
    wait = int(config.compose_timeout)
    started = await box.run(
        f"{base} up --detach --wait --wait-timeout {wait} --quiet-pull 2>&1",
        stdin=None,
        timeout=config.compose_timeout + 60,
        limit=1 << 22,
    )
    running = await box.run(
        f"{base} ps --status running --format '{{{{.Service}}}}'", stdin=None, timeout=120, limit=1 << 20
    )
    services = set(_text(running.stdout).split())
    if source.default_service not in services:
        raise RuntimeError(
            f"The compose service '{source.default_service}' didn't start in Orble box {box.name}.\n"
            f"{_text(started.stdout)[-4000:]}"
        )
    environments: dict[str, SandboxEnvironment] = {}
    for service in [source.default_service] + [
        s for s in source.services if s != source.default_service and s in services
    ]:
        compose_service = cmds.Compose(project.project, project.directory, project.file, service)
        working_dir = config.working_dir
        if working_dir is None:
            pwd = await box.run(
                shlex.join(cmds.script_argv(cmds.PWD, user=None, compose=compose_service)),
                stdin=None,
                timeout=120,
                limit=1 << 16,
            )
            printed = _text(pwd.stdout).strip()
            working_dir = printed if pwd.exit_code == 0 and printed else "/"
        environments[service] = OrbleSandboxEnvironment(box, working_dir, compose_service)
    return environments


async def _delete(name: str) -> None:
    """Delete a box this process made; a failure only warns. Not cancellable."""
    with anyio.CancelScope(shield=True):
        try:
            await _api().delete_box(name)
            trace_message(logger, "orble", f"Deleted box {name}")
        except Exception as error:  # noqa: BLE001  # a failed delete must not fail the eval; the backstop remains
            logger.warning(f"Couldn't delete Orble box {name}: {error}")
            return
    _run().boxes.discard(name)


def _signal_number(name: str) -> int:
    import signal

    with contextlib.suppress(AttributeError):
        return int(getattr(signal, f"SIG{name}"))
    return 0


def _check_user(user: str | None) -> None:
    if user == "":
        raise SandboxUserUnsupportedError("The user must not be empty.")


def _text(data: bytes | bytearray | str) -> str:
    return data if isinstance(data, str) else bytes(data).decode("utf-8", errors="replace")


def _raise_file_error(stderr: str, exit_code: int, file: str, user: str | None) -> None:
    message = stderr.casefold()
    if exit_code == 21 or "is a directory" in message:
        raise IsADirectoryError(errno.EISDIR, "Is a directory", file)
    if "no such file" in message or "not a directory" in message:
        raise FileNotFoundError(errno.ENOENT, "No such file or directory", file)
    if "permission denied" in message or "operation not permitted" in message:
        raise PermissionError(errno.EACCES, "Permission denied", file)
    if user is not None and ("unknown user" in message or "unable to find user" in message):
        raise SandboxUserUnsupportedError(f"No user {user!r} in the sandbox: {stderr.strip()}")
    raise RuntimeError(f"File operation on {file} failed (exit {exit_code}): {stderr.strip()}")
