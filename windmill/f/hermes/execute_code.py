"""Invoke Hermes tool: execute_code — path: f/hermes/execute_code"""
from f.hermes.client import hermes_endpoint, run_tool


def main(hermes: hermes_endpoint, code: str = "") -> str:
    return run_tool(hermes, "execute_code", {"code": code})
