"""Invoke Hermes tool: memory — path: f/hermes/memory"""
from f.hermes.client import hermes_endpoint, run_tool


def main(
    hermes: hermes_endpoint,
    action: str = "add",
    target: str = "memory",
    content: str = None,
    old_text: str = None,
    new_text: str = None,
    operations: list[dict] = None,
) -> str:
    args: dict = {"action": action, "target": target}
    for k in ("content", "old_text", "new_text"):
        v = locals()[k]
        if v is not None:
            args[k] = v
    if operations:
        args["operations"] = operations
    return run_tool(hermes, "memory", args)
