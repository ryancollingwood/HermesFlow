"""Invoke Hermes tool: read_file — path: f/hermes/read_file"""
from f.hermes.client import hermes_endpoint, run_tool


def main(hermes: hermes_endpoint, path: str = "", offset: int = None, limit: int = None) -> str:
    args: dict = {"path": path}
    if offset is not None:
        args["offset"] = offset
    if limit is not None:
        args["limit"] = limit
    return run_tool(hermes, "read_file", args)
