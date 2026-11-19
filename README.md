# orble-inspect

[Inspect AI](https://inspect.aisi.org.uk) sandboxes on [Orble](https://orble.tech) boxes: each sample gets its own
Firecracker VM, deleted when the sample ends.

```sh
pip install orble-inspect
export ORBLE_API_KEY=orble_...    # orble.tech/settings?tab=api
```

```python
from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import includes
from inspect_ai.solver import generate, use_tools
from inspect_ai.tool import bash


@task
def hello():
    return Task(
        dataset=[Sample(input="What's the kernel version? Use bash.", target="6.")],
        solver=[use_tools(bash()), generate()],
        scorer=includes(),
        sandbox="orble",
    )
```

```sh
inspect eval hello.py --model anthropic/claude-sonnet-4-5
```

A box is Debian 13 with Docker, Python and Node; commands run as `agent`, who has passwordless sudo.

## Settings

```python
from orble_inspect import OrbleSandboxEnvironmentConfig

sandbox=("orble", OrbleSandboxEnvironmentConfig(size="m", snapshot="my-env"))
```

| Setting | Default | |
|---|---|---|
| `size` | `s` | `s` 2 vCPU 4 GiB, `m` 4/8, `l` 8/16 |
| `snapshot` | none | start each sample from this snapshot |
| `compose` | none | a `compose.yaml` or `Dockerfile` to run with Docker in the box |
| `working_dir` | `/home/agent` | where commands run and relative paths resolve |
| `name_prefix` | `inspect` | box names are `<prefix>-<random>` |
| `delete_after` | 6 hours | the box deletes itself after this many seconds, as a backstop |
| `compose_timeout` | 900 | seconds `docker compose up` may take |

`sandbox=("orble", "settings.json")` reads the same settings from a file.

## Dockerfiles and compose files

`sandbox=("orble", "compose.yaml")` or `("orble", "Dockerfile")` runs it unchanged with Docker inside the box, as
Inspect's Docker sandbox would; a task that already has a `compose.yaml` next to it works with `sandbox="orble"`.
Several services share the box.

Every sample gets a fresh box, so Docker builds the image again in each one: 40 to 70 s per sample for a small
Ubuntu image. Faster: build the Dockerfile into a snapshot once, and start every sample from it. The snapshot's
Dockerfile starts `FROM orble.tech/base`, and there's no Docker layer between the eval and the VM:

```sh
orble snapshot build -f Dockerfile my-env
```

```python
sandbox=("orble", OrbleSandboxEnvironmentConfig(snapshot="my-env"))
```

Time from asking for a sandbox to its first command, on Orble's staging servers:

| | Start |
|---|---|
| Orble's image | 0.8 s |
| From a snapshot | 0.7 s |
| A Dockerfile run with Docker in the box | 40 to 70 s |

## How it works

- Each sample creates a box through Orble's API and deletes it when the sample ends. Boxes are kept awake while in
  use, and each one deletes itself after `delete_after` in case the eval is killed.
- Commands and files go over SSH, tunnelled through Orble's API over HTTPS, so IPv6 isn't needed. Each eval adds a
  fresh SSH key to the account and removes it when it ends.
- `inspect sandbox cleanup orble` lists and deletes boxes named `inspect-...`, and with none left, removes the SSH
  keys of evals that were killed; `inspect sandbox cleanup orble <box>` deletes one box.

## Tests

```sh
pip install -e '.[test]'
pytest tests/test_commands.py                     # no box needed
ORBLE_API_KEY=... pytest tests/test_self_check.py  # Inspect's sandbox checks on a real box
```

MIT licence.
