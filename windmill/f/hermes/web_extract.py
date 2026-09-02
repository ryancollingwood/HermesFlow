"""Invoke Hermes tool: web_extract — path: f/hermes/web_extract"""
from f.hermes.client import hermes_endpoint, run_tool


def main(hermes: hermes_endpoint, urls: list[str] = None, char_limit: int = None) -> str:
    args: dict = {"urls": urls or []}
    if char_limit:
        args["char_limit"] = char_limit
    return run_tool(hermes, "web_extract", args)
