"""Invoke Hermes tool: process — path: f/hermes/process"""
from f.hermes.client import hermes_endpoint, run_tool


ACTIONS = ["list", "poll", "log", "wait", "kill", "write", "submit", "close"]


def main(
    hermes: hermes_endpoint,
    action: str = "list",
    session_id: str = None,
    data: str = None,
    timeout: int = None,
    offset: int = None,
    limit: int = None,
) -> str:
    if action not in ACTIONS:
        raise ValueError(f"Unknown process action '{action}'. Known: {', '.join(ACTIONS)}")
    args: dict = {"action": action}
    if session_id:
        args["session_id"] = session_id
    if data is not None:
        args["data"] = data
    if timeout is not None:
        args["timeout"] = timeout
    if offset is not None:
        args["offset"] = offset
    if limit is not None:
        args["limit"] = limit
    return run_tool(hermes, "process", args)
