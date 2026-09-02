"""
Generic Hermes tool dispatcher — path: f/hermes/tool

Calls ANY tool in the f/hermes/client registry server-side and returns the raw
output. Known tools:
    web_search, web_extract,
    browser_navigate, browser_click, browser_type, browser_scroll, browser_back,
    browser_press, browser_cdp, browser_exec, browser_console, browser_dialog,
    browser_snapshot, browser_get_images, browser_vision,
    terminal, process, read_file, write_file, patch, search_files, execute_code,
    skill_view, skill_manage, skills_list, memory, delegate_task, cronjob,
    session_search, todo, image_generate, vision_analyze
"""
from f.hermes.client import hermes_endpoint, run_tool, TOOL_SCHEMAS


def main(
    hermes: hermes_endpoint,
    tool: str = "web_search",
    arguments: dict = None,
) -> str:
    if tool not in TOOL_SCHEMAS:
        raise ValueError(f"Unknown tool '{tool}'. Known: {', '.join(sorted(TOOL_SCHEMAS))}")
    return run_tool(hermes, tool, arguments or {})
