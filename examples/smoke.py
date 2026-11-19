"""A small eval that needs no model API: each sample writes a file in its own box, runs a command and checks the
answer. Run it with: inspect eval examples/smoke.py --model mockllm/model"""

from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import CORRECT, INCORRECT, Score, Target, accuracy, scorer
from inspect_ai.solver import Generate, TaskState, solver
from inspect_ai.util import sandbox


@solver
def compute():
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        await sandbox().write_file("input.txt", state.input_text)
        result = await sandbox().exec(["sh", "-c", "python3 -c \"print(eval(open('input.txt').read()))\"; hostname"])
        state.output.completion = result.stdout
        return state

    return solve


@scorer(metrics=[accuracy()])
def first_line():
    async def score(state: TaskState, target: Target) -> Score:
        lines = state.output.completion.splitlines()
        return Score(value=CORRECT if lines and lines[0] == target.text else INCORRECT, answer=lines[0] if lines else "",
                     explanation=f"box {lines[1] if len(lines) > 1 else '?'}")

    return score


@task
def smoke():
    return Task(
        dataset=[Sample(input="6*7", target="42"), Sample(input="2**10", target="1024"),
                 Sample(input="sum(range(101))", target="5050"), Sample(input="len('orble')", target="5")],
        solver=compute(),
        scorer=first_line(),
        sandbox="orble",
    )
