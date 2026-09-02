"""Invoke Hermes tool: session_search — path: f/hermes/session_search"""
from f.hermes.client import hermes_endpoint, run_tool


def main(
    hermes: hermes_endpoint,
    query: str = None,
    limit: int = None,
    sort: str = None,
    detail: str = None,
    session_id: str = None,
    around_message_id: int = None,
    window: int = None,
    role_filter: str = None,
    profile: str = None,
) -> str:
    args: dict = {}
    for k in ("query", "limit", "sort", "detail", "session_id", "around_message_id",
              "window", "role_filter", "profile"):
        v = locals()[k]
        if v is not None:
            args[k] = v
    return run_tool(hermes, "session_search", args)
