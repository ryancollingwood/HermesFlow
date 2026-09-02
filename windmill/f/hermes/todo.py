"""Invoke Hermes tool: todo — path: f/hermes/todo"""
from f.hermes.client import hermes_endpoint, run_tool


def main(hermes: hermes_endpoint, todos: list[dict] = None, merge: bool = False) -> str:
    args: dict = {}
    if todos:
        args["todos"] = todos
    if merge:
        args["merge"] = True
    return run_tool(hermes, "todo", args)
