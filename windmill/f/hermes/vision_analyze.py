"""Invoke Hermes tool: vision_analyze — path: f/hermes/vision_analyze"""
from f.hermes.client import hermes_endpoint, run_tool


def main(hermes: hermes_endpoint, path: str = "", question: str = "Describe this image") -> str:
    return run_tool(hermes, "vision_analyze", {"path": path, "question": question})
