"""Invoke Hermes tool: cronjob — path: f/hermes/cronjob"""
from f.hermes.client import hermes_endpoint, run_tool

ACTIONS = ["create", "list", "update", "pause", "resume", "remove", "run"]


def main(
    hermes: hermes_endpoint,
    action: str = "list",
    job_id: str = None,
    schedule: str = None,
    prompt: str = None,
    name: str = None,
    repeat: int = None,
    deliver: str = None,
    skills: list[str] = None,
    script: str = None,
    monitor_script: str = None,
    monitor_url: str = None,
    no_agent: bool = False,
    context_from: list[str] = None,
    enabled_toolsets: list[str] = None,
    workdir: str = None,
    attach_to_session: bool = False,
) -> str:
    if action not in ACTIONS:
        raise ValueError(f"Unknown cronjob action '{action}'. Known: {', '.join(ACTIONS)}")
    args: dict = {"action": action}
    for k in ("job_id", "schedule", "prompt", "name", "repeat", "deliver", "skills",
              "script", "monitor_script", "monitor_url", "context_from", "enabled_toolsets", "workdir"):
        v = locals()[k]
        if v is not None:
            args[k] = v
    if no_agent:
        args["no_agent"] = True
    if attach_to_session:
        args["attach_to_session"] = True
    return run_tool(hermes, "cronjob", args)
