"""Invoke Hermes tool: patch — path: f/hermes/patch"""
from f.hermes.client import hermes_endpoint, run_tool


def main(
    hermes: hermes_endpoint,
    mode: str = "replace",
    path: str = None,
    old_string: str = None,
    new_string: str = None,
    replace_all: bool = False,
    patch: str = None,
) -> str:
    args: dict = {"mode": mode}
    if path is not None:
        args["path"] = path
    if old_string is not None:
        args["old_string"] = old_string
    if new_string is not None:
        args["new_string"] = new_string
    if replace_all:
        args["replace_all"] = True
    if patch is not None:
        args["patch"] = patch
    return run_tool(hermes, "patch", args)
