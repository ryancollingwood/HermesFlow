"""Invoke Hermes tool: skill_view — path: f/hermes/skill_view"""
from f.hermes.client import hermes_endpoint, run_tool


def main(hermes: hermes_endpoint, name: str = "", file_path: str = None) -> str:
    args: dict = {"name": name}
    if file_path is not None:
        args["file_path"] = file_path
    return run_tool(hermes, "skill_view", args)
