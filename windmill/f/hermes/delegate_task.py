"""Invoke Hermes tool: delegate_task — path: f/hermes/delegate_task"""
from f.hermes.client import hermes_endpoint, run_tool

ACTIONS = ["spawn", "list", "steer", "stop"]


def main(
    hermes: hermes_endpoint,
    action: str = "spawn",
    goal: str = None,
    context: str = None,
    tasks: list[dict] = None,
    role: str = None,
    subagent_id: str = None,
    message: str = None,
) -> str:
    if action not in ACTIONS:
        raise ValueError(f"Unknown delegate action '{action}'. Known: {', '.join(ACTIONS)}")
    args: dict = {"action": action}
    for k in ("goal", "context", "tasks", "role", "subagent_id", "message"):
        v = locals()[k]
        if v is not None:
            args[k] = v
    return run_tool(hermes, "delegate_task", args)
