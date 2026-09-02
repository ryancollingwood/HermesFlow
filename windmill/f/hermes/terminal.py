"""Invoke Hermes tool: terminal — path: f/hermes/terminal"""
from f.hermes.client import hermes_endpoint, run_tool


def main(
    hermes: hermes_endpoint,
    command: str = "",
    timeout: int = None,
    workdir: str = None,
    background: bool = False,
    pty: bool = False,
    notify_on_complete: bool = False,
    watch_patterns: list[str] = None,
) -> str:
    args: dict = {"command": command, "background": background, "pty": pty,
                  "notify_on_complete": notify_on_complete}
    if timeout is not None:
        args["timeout"] = timeout
    if workdir:
        args["workdir"] = workdir
    if watch_patterns:
        args["watch_patterns"] = watch_patterns
    return run_tool(hermes, "terminal", args)
