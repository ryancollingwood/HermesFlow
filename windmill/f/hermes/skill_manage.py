"""Invoke Hermes tool: skill_manage — path: f/hermes/skill_manage"""
from f.hermes.client import hermes_endpoint, run_tool

ACTIONS = ["create", "patch", "edit", "delete", "write_file", "remove_file"]


def main(
    hermes: hermes_endpoint,
    action: str = "patch",
    name: str = "",
    content: str = None,
    old_string: str = None,
    new_string: str = None,
    replace_all: bool = False,
    category: str = None,
    file_path: str = None,
    file_content: str = None,
    absorbed_into: str = None,
) -> str:
    if action not in ACTIONS:
        raise ValueError(f"Unknown skill_manage action '{action}'. Known: {', '.join(ACTIONS)}")
    args: dict = {"action": action, "name": name}
    for k in ("content", "old_string", "new_string", "category", "file_path", "file_content", "absorbed_into"):
        v = locals()[k]
        if v is not None:
            args[k] = v
    if replace_all:
        args["replace_all"] = True
    return run_tool(hermes, "skill_manage", args)
