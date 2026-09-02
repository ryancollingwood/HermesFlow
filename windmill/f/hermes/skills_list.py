"""Invoke Hermes tool: skills_list — path: f/hermes/skills_list"""
from f.hermes.client import hermes_endpoint, run_tool


def main(hermes: hermes_endpoint, category: str = None) -> str:
    args: dict = {}
    if category:
        args["category"] = category
    return run_tool(hermes, "skills_list", args)
