"""Invoke Hermes tool: web_search — path: f/hermes/web_search"""
from f.hermes.client import hermes_endpoint, run_tool


def main(hermes: hermes_endpoint, query: str = "", limit: int = 5) -> str:
    return run_tool(hermes, "web_search", {"query": query, "limit": limit})
