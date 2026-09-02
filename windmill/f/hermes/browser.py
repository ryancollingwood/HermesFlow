"""
Invoke Hermes browser toolset — path: f/hermes/browser

Primary use: action='exec' (default) runs a browser-use automation snippet
(the robust path). Other actions map to the individual browser_* tools:
navigate, click, type, scroll, back, press, cdp, console, dialog, snapshot,
get_images, vision.

See the `browser-exec` skill for the snippet DSL (new_tab, goto_url, js,
fill_input, click_at_xy, page_info, capture_screenshot, ...).
"""
from f.hermes.client import hermes_endpoint, run_tool

BROWSER_ACTIONS = ["exec", "navigate", "click", "type", "scroll", "back", "press",
                   "cdp", "console", "dialog", "snapshot", "get_images", "vision"]


def main(
    hermes: hermes_endpoint,
    action: str = "exec",
    arguments: dict = None,
    session: str = None,
    timeout_s: int = None,
) -> str:
    if action not in BROWSER_ACTIONS:
        raise ValueError(f"Unknown browser action '{action}'. Known: {', '.join(BROWSER_ACTIONS)}")
    tool = "browser_exec" if action == "exec" else f"browser_{action}"
    args: dict = dict(arguments or {})
    if session:
        args["session"] = session
    if timeout_s is not None:
        args["timeout_s"] = timeout_s
    return run_tool(hermes, tool, args)
