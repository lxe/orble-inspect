"""The shell lines, checked without a box."""

import pytest

from orble_inspect import OrbleSandboxEnvironmentConfig
from orble_inspect import _commands as c
from orble_inspect._config import resolve_config


def test_exec_plain():
    argv = c.exec_argv(["echo", "a b"], cwd="/home/agent", env=None, user=None, timeout=None, compose=None)
    assert argv == ["/bin/sh", "-c", 'cd -- "$0" && exec "$@"', "/home/agent", "echo", "a b"]


def test_exec_user_env_timeout():
    argv = c.exec_argv(["id"], cwd="/tmp", env={"A": "1 2"}, user="root", timeout=7, compose=None)
    assert argv[:6] == ["/usr/bin/sudo", "-n", "-H", "-u", "root", "--"]
    assert argv[6:10] == ["/usr/bin/timeout", "-k", "5s", "7s"]
    assert argv[-3:] == ["/usr/bin/env", "A=1 2", "id"]


def test_exec_uid():
    argv = c.exec_argv(["id"], cwd="/", env=None, user="1001", timeout=None, compose=None)
    assert "#1001" in argv


def test_exec_compose():
    compose = c.Compose("p", "/d", "/d/compose.yaml", "web")
    argv = c.exec_argv(["ls"], cwd="/app", env={"X": "y"}, user="u", timeout=3, compose=compose)
    assert argv[:9] == [
        "docker",
        "compose",
        "--project-name",
        "p",
        "--project-directory",
        "/d",
        "--file",
        "/d/compose.yaml",
        "exec",
    ]
    assert argv[-6:] == ["web", "/usr/bin/timeout", "-k", "5s", "3s", "ls"]
    assert argv[10:12] == ["--workdir", "/app"] and "--user" in argv and "X=y" in argv


def test_bad_env_name():
    with pytest.raises(ValueError):
        c.exec_argv(["true"], cwd="/", env={"1A": "x"}, user=None, timeout=None, compose=None)


def test_empty_cmd():
    with pytest.raises(ValueError):
        c.exec_argv([], cwd="/", env=None, user=None, timeout=None, compose=None)


def test_timeout_without_user_wraps_the_shell():
    argv = c.exec_argv(["sleep", "9"], cwd="/", env=None, user=None, timeout=3, compose=None)
    assert argv[:5] == ["/usr/bin/timeout", "-k", "5s", "3s", "/bin/sh"]


def test_script_pins_path():
    argv = c.script_argv(c.READ, "/f", "10", user="bob", compose=None)
    assert argv[:6] == ["/usr/bin/sudo", "-n", "-H", "-u", "bob", "--"]
    assert argv[8].startswith("PATH=/usr/local/sbin")


def test_config_checks():
    for bad in (
        {"size": "xl"},
        {"name_prefix": "Bad"},
        {"working_dir": "rel"},
        {"delete_after": 5},
        {"compose": "notes.txt"},
    ):
        with pytest.raises(ValueError):
            OrbleSandboxEnvironmentConfig(**bad).check()
    resolved, compose = resolve_config(None)
    assert resolved.size is None and compose is None
    with pytest.raises(TypeError):
        resolve_config(42)


def test_config_hashable():
    assert hash(OrbleSandboxEnvironmentConfig(size="m")) == hash(OrbleSandboxEnvironmentConfig(size="m"))


def test_compose_archive_leaves_out_secrets_and_logs(tmp_path):
    import io
    import tarfile

    from orble_inspect._compose import compose_source

    (tmp_path / "Dockerfile").write_text("FROM ubuntu:24.04\n")
    (tmp_path / ".env").write_text("ORBLE_API_KEY=secret\n")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "eval.json").write_text("{}")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "logs").mkdir()
    (tmp_path / "app" / "logs" / "keep.txt").write_text("kept")
    source = compose_source(str(tmp_path / "Dockerfile"))
    with tarfile.open(fileobj=io.BytesIO(source.archive)) as tar:
        names = {n.lstrip("./") for n in tar.getnames()}
    assert "Dockerfile" in names and "app/logs/keep.txt" in names
    assert ".env" not in names and not any(n.startswith("logs") for n in names)
