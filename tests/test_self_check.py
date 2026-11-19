"""Inspect's own sandbox conformance checks, on real Orble boxes. Needs ORBLE_API_KEY (and ORBLE_API_URL for a
non-production API); skipped without it.

The checks run three times: in a plain box, in a Dockerfile's container run with Docker inside the box, and, when
ORBLE_INSPECT_SNAPSHOT names one, in a box started from that snapshot. Each sample's start time is printed.
"""

import os
import time
from pathlib import Path

import pytest
from inspect_ai.util._sandbox.self_check import *

from orble_inspect import OrbleSandboxEnvironment, OrbleSandboxEnvironmentConfig

pytestmark = pytest.mark.skipif(not os.environ.get("ORBLE_API_KEY"), reason="ORBLE_API_KEY not set")

CONFIGS = {
    "box": OrbleSandboxEnvironmentConfig(),
    "dockerfile": OrbleSandboxEnvironmentConfig(compose=str(Path(__file__).parent / "fixtures" / "Dockerfile")),
}
if os.environ.get("ORBLE_INSPECT_SNAPSHOT"):
    CONFIGS["snapshot"] = OrbleSandboxEnvironmentConfig(snapshot=os.environ["ORBLE_INSPECT_SNAPSHOT"])


@pytest.fixture(scope="module", params=list(CONFIGS))
async def box(request):
    config = CONFIGS[request.param]
    await OrbleSandboxEnvironment.task_init("self_check", config)
    started = time.monotonic()
    envs = await OrbleSandboxEnvironment.sample_init("self_check", config, {})
    print(f"\n{request.param}: sample started in {time.monotonic() - started:.1f} s")
    yield envs["default"]
    await OrbleSandboxEnvironment.sample_cleanup("self_check", config, envs, False)
    await OrbleSandboxEnvironment.task_cleanup("self_check", config, True)


@pytest.fixture
async def sandbox_env(box):
    return box
