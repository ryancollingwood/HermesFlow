"""Invoke Hermes tool: search_files — path: f/hermes/search_files"""
from f.hermes.client import hermes_endpoint, run_tool


def main(
    hermes: hermes_endpoint,
    pattern: str = "",
    target: str = "content",
    path: str = None,
    file_glob: str = None,
    limit: int = None,
    output_mode: str = None,
    context: int = None,
    offset: int = None,
) -> str:
    args: dict = {"pattern": pattern, "target": target}
    if path is not None:
        args["path"] = path
    if file_glob is not None:
        args["file_glob"] = file_glob
    if limit is not None:
        args["limit"] = limit
    if output_mode is not None:
        args["output_mode"] = output_mode
    if context is not None:
        args["context"] = context
    if offset is not None:
        args["offset"] = offset
    return run_tool(hermes, "search_files", args)
