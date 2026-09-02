"""Invoke Hermes tool: image_generate — path: f/hermes/image_generate"""
from f.hermes.client import hermes_endpoint, run_tool


def main(hermes: hermes_endpoint, prompt: str = "", size: str = None, count: int = None) -> str:
    args: dict = {"prompt": prompt}
    if size:
        args["size"] = size
    if count is not None:
        args["count"] = count
    return run_tool(hermes, "image_generate", args)
