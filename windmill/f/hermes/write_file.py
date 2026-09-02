"""Invoke Hermes tool: write_file — path: f/hermes/write_file"""
from f.hermes.client import hermes_endpoint, run_tool


def main(hermes: hermes_endpoint, path: str = "", content: str = "") -> str:
    return run_tool(hermes, "write_file", {"path": path, "content": content})
