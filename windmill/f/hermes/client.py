"""
Shared Hermes client for Windmill — path: f/hermes/client

Other scripts reuse the helpers from here:

    from f.hermes.client import hermes_endpoint, chat, run_tool, TOOL_SCHEMAS

`hermes_endpoint` is a Windmill RESOURCE TYPE (base_url + api_key). Create one
resource of this type (e.g. f/hermes/local) and pass it into any script — no
base URL or key hardcoded in the scripts themselves.

`run_tool(conn, tool, arguments)` invokes a single Hermes tool SERVER-SIDE and
returns its RAW output. It posts to /v1/chat/completions with the tool's schema
and `tool_choice` forced to that tool; Hermes executes the tool on the gateway
host and returns the result verbatim (no caller-side replay loop). The set of
tools comes from /v1/toolsets; the parameter contracts live in TOOL_SCHEMAS.

Running THIS script directly returns the list of models the gateway serves,
which doubles as a connectivity test.
"""
import json
import urllib.request
from typing import TypedDict

from openai import OpenAI


class hermes_endpoint(TypedDict):
    base_url: str
    api_key: str


# Hermes's OpenAI-compatible API server runs every request as a full agent
# turn with the `api_server` platform's toolset (including memory-write
# tools) available — there's no per-request way to scope that down, and it's
# shared with the Hermes dashboard, so it can't be disabled platform-wide.
# Append this to any system prompt sent from Windmill so the agent doesn't
# mistake pipeline content for something a person said and worth retaining.
NO_MEMORY_GUARD = (
    "\n\nIMPORTANT: this request was generated programmatically by a "
    "Windmill job, not typed by a person. Do not call hindsight_retain, "
    "hindsight_recall, hindsight_reflect, or any other memory tool for this "
    "request, and do not infer or record anything from this exchange into "
    "long-term memory — nothing here reflects a person's statements, "
    "preferences, or identity."
)


def get_client(conn: hermes_endpoint) -> OpenAI:
    return OpenAI(base_url=conn["base_url"], api_key=conn["api_key"])


def chat(
    conn: hermes_endpoint,
    prompt: str,
    model: str = "hermes",
    system: str = "You are a concise assistant.",
    temperature: float = 0.7,
) -> str:
    """Single chat completion against Hermes; returns the text."""
    resp = get_client(conn).chat.completions.create(
        model=model,
        temperature=temperature,
        messages=[
            {"role": "system", "content": system + NO_MEMORY_GUARD},
            {"role": "user", "content": prompt},
        ],
    )
    return resp.choices[0].message.content


# ---------------------------------------------------------------------------
# Tool schema registry (mirrors /v1/toolsets on the api_server platform)
# ---------------------------------------------------------------------------
def _p(props: dict, required: tuple = (), description: str = ""):
    return {
        "description": description,
        "parameters": {
            "type": "object",
            "properties": props,
            "required": list(required),
        },
    }


TOOL_SCHEMAS = {
    # --- web ---
    "web_search": _p(
        {"query": {"type": "string", "description": "Search query (supports site:, filetype:, -term, quoted phrases)"},
         "limit": {"type": "integer", "description": "Max results (default 5)"}},
        ("query",), "Search the web"),
    "web_extract": _p(
        {"urls": {"type": "array", "items": {"type": "string"}, "maxItems": 5, "description": "URLs to extract (max 5)"},
         "char_limit": {"type": "integer", "description": "Per-page char budget"}},
        ("urls",), "Extract clean content from web pages"),
    # --- browser ---
    "browser_navigate": _p(
        {"url": {"type": "string"}, "session": {"type": "string"}}, ("url",), "Open a URL in the browser"),
    "browser_click": _p(
        {"selector": {"type": "string"}, "x": {"type": "integer"}, "y": {"type": "integer"},
         "session": {"type": "string"}}, (), "Click an element (selector or coordinates)"),
    "browser_type": _p(
        {"selector": {"type": "string"}, "text": {"type": "string"}, "session": {"type": "string"}},
        ("selector", "text"), "Type text into an input"),
    "browser_scroll": _p(
        {"x": {"type": "integer"}, "y": {"type": "integer"}, "direction": {"type": "string"},
         "session": {"type": "string"}}, (), "Scroll the page"),
    "browser_back": _p({"session": {"type": "string"}}, (), "Go back in history"),
    "browser_press": _p(
        {"key": {"type": "string"}, "session": {"type": "string"}}, ("key",), "Press a key"),
    "browser_cdp": _p(
        {"method": {"type": "string"}, "params": {"type": "object"}, "session": {"type": "string"}},
        ("method",), "Raw Chrome DevTools Protocol call"),
    "browser_exec": _p(
        {"code": {"type": "string", "description": "Python using pre-imported browser helpers (new_tab, js, fill_input, click_at_xy, page_info, capture_screenshot...)"},
         "session": {"type": "string"}, "timeout_s": {"type": "integer"}},
        ("code",), "Run a browser-use automation snippet"),
    "browser_console": _p(
        {"expression": {"type": "string"}, "session": {"type": "string"}}, (), "Read JS console"),
    "browser_dialog": _p(
        {"action": {"type": "string"}, "session": {"type": "string"}}, (), "Handle a dialog"),
    "browser_snapshot": _p({"session": {"type": "string"}}, (), "Snapshot the page state"),
    "browser_get_images": _p({"session": {"type": "string"}}, (), "List images on the page"),
    "browser_vision": _p(
        {"question": {"type": "string"}, "path": {"type": "string"}, "session": {"type": "string"}}, ("question",),
        "Ask a vision question about the page"),
    # --- terminal ---
    "terminal": _p(
        {"command": {"type": "string", "description": "Shell command to run"},
         "timeout": {"type": "integer"}, "workdir": {"type": "string"}, "background": {"type": "boolean"},
         "pty": {"type": "boolean"}, "notify_on_complete": {"type": "boolean"},
         "watch_patterns": {"type": "array", "items": {"type": "string"}}},
        ("command",), "Run a shell command"),
    "process": _p(
        {"action": {"type": "string", "enum": ["list", "poll", "log", "wait", "kill", "write", "submit", "close"]},
         "session_id": {"type": "string"}, "data": {"type": "string"}, "timeout": {"type": "integer"},
         "offset": {"type": "integer"}, "limit": {"type": "integer"}},
        ("action",), "Manage background processes"),
    # --- file ---
    "read_file": _p(
        {"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}},
        ("path",), "Read a file with line numbers"),
    "write_file": _p(
        {"path": {"type": "string"}, "content": {"type": "string"}, "cross_profile": {"type": "boolean"}},
        ("path", "content"), "Write a file (overwrites)"),
    "patch": _p(
        {"mode": {"type": "string", "enum": ["replace", "patch"]}, "path": {"type": "string"},
         "old_string": {"type": "string"}, "new_string": {"type": "string"}, "replace_all": {"type": "boolean"},
         "patch": {"type": "string"}, "cross_profile": {"type": "boolean"}},
        (), "Find-and-replace / apply a patch to a file"),
    "search_files": _p(
        {"pattern": {"type": "string"}, "target": {"type": "string", "enum": ["content", "files"]},
         "path": {"type": "string"}, "file_glob": {"type": "string"}, "limit": {"type": "integer"},
         "output_mode": {"type": "string", "enum": ["content", "files_only", "count"]},
         "context": {"type": "integer"}, "offset": {"type": "integer"}},
        ("pattern",), "Search file contents or find files"),
    # --- code_execution ---
    "execute_code": _p({"code": {"type": "string"}}, ("code",), "Run Python that calls Hermes tools programmatically"),
    # --- skills ---
    "skill_view": _p(
        {"name": {"type": "string"}, "file_path": {"type": "string"}}, ("name",), "Load a skill's SKILL.md"),
    "skill_manage": _p(
        {"action": {"type": "string", "enum": ["create", "patch", "edit", "delete", "write_file", "remove_file"]},
         "name": {"type": "string"}, "content": {"type": "string"}, "old_string": {"type": "string"},
         "new_string": {"type": "string"}, "replace_all": {"type": "boolean"}, "category": {"type": "string"},
         "file_path": {"type": "string"}, "file_content": {"type": "string"},
         "absorbed_into": {"type": "string"}},
        ("action", "name"), "Create/update/delete a skill"),
    "skills_list": _p({"category": {"type": "string"}}, (), "List available skills"),
    # --- memory ---
    "memory": _p(
        {"target": {"type": "string", "enum": ["memory", "user"]},
         "action": {"type": "string", "enum": ["add", "replace", "remove"]},
         "content": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"},
         "operations": {"type": "array", "items": {"type": "object"}}},
        (), "Read/write long-term memory"),
    # --- delegation ---
    "delegate_task": _p(
        {"goal": {"type": "string"}, "context": {"type": "string"},
         "tasks": {"type": "array", "items": {"type": "object"}},
         "role": {"type": "string", "enum": ["leaf", "orchestrator"]}, "background": {"type": "boolean"},
         "action": {"type": "string", "enum": ["spawn", "list", "steer", "stop"]},
         "subagent_id": {"type": "string"}, "message": {"type": "string"},
         "output_schema": {"type": "object"}},
        (), "Spawn/delegate work to subagents"),
    # --- cronjob ---
    "cronjob": _p(
        {"action": {"type": "string", "enum": ["create", "list", "update", "pause", "resume", "remove", "run"]},
         "job_id": {"type": "string"}, "schedule": {"type": "string"}, "prompt": {"type": "string"},
         "name": {"type": "string"}, "repeat": {"type": "integer"}, "deliver": {"type": "string"},
         "skills": {"type": "array", "items": {"type": "string"}}, "script": {"type": "string"},
         "monitor_script": {"type": "string"}, "monitor_url": {"type": "string"},
         "no_agent": {"type": "boolean"}, "context_from": {"type": "array", "items": {"type": "string"}},
         "enabled_toolsets": {"type": "array", "items": {"type": "string"}}, "workdir": {"type": "string"},
         "attach_to_session": {"type": "boolean"}},
        ("action",), "Manage scheduled cron jobs"),
    # --- session_search ---
    "session_search": _p(
        {"query": {"type": "string"}, "limit": {"type": "integer"}, "sort": {"type": "string", "enum": ["newest", "oldest"]},
         "detail": {"type": "string"}, "session_id": {"type": "string"}, "around_message_id": {"type": "integer"},
         "window": {"type": "integer"}, "role_filter": {"type": "string"}, "profile": {"type": "string"}},
        (), "Search past Hermes sessions"),
    # --- todo ---
    "todo": _p(
        {"todos": {"type": "array", "items": {"type": "object"}}, "merge": {"type": "boolean"}},
        (), "Manage the task list"),
    # --- image_gen ---
    "image_generate": _p(
        {"prompt": {"type": "string"}, "size": {"type": "string"}, "count": {"type": "integer"}},
        ("prompt",), "Generate an image from a prompt"),
    # --- vision ---
    "vision_analyze": _p(
        {"path": {"type": "string", "description": "Local path or URL of the image"},
         "question": {"type": "string"}},
        ("path", "question"), "Analyze an image with vision"),
}


# ---------------------------------------------------------------------------
# run_tool — force a single Hermes tool and return its RAW output
# ---------------------------------------------------------------------------
def run_tool(
    conn: hermes_endpoint,
    tool: str,
    arguments: dict | None = None,
    system: str | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
) -> str:
    """Invoke one Hermes tool server-side and return its raw output verbatim.

    Force-calls the tool via tool_choice and instructs the agent to return the
    tool result unchanged. Supports every tool in TOOL_SCHEMAS.
    """
    if tool not in TOOL_SCHEMAS:
        raise ValueError(f"Unknown tool '{tool}'. Known: {', '.join(sorted(TOOL_SCHEMAS))}")
    s = TOOL_SCHEMAS[tool]
    fn = {
        "type": "function",
        "function": {"name": tool, "description": s["description"], "parameters": s["parameters"]},
    }
    sys_msg = system or (
        "You are a strict tool executor. Call the requested tool EXACTLY once with the "
        "provided arguments and return the tool's raw output verbatim. Do not summarize, "
        "paraphrase, or add commentary."
    )
    user_msg = (
        f"Execute the Hermes tool '{tool}' exactly once with these arguments:\n"
        + json.dumps(arguments or {}, ensure_ascii=False)
        + "\nCall the tool now and return its RAW output verbatim, with no commentary."
    )
    body = {
        "model": "hermes-agent",
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": sys_msg + NO_MEMORY_GUARD},
            {"role": "user", "content": user_msg},
        ],
        "tools": [fn],
        "tool_choice": {"type": "function", "function": {"name": tool}},
    }
    if max_tokens:
        body["max_tokens"] = max_tokens
    req = urllib.request.Request(
        conn["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {conn['api_key']}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.loads(r.read())
    return data["choices"][0]["message"]["content"]


def main(conn: hermes_endpoint) -> list[str]:
    """Connectivity test: list the models the gateway serves."""
    return [m.id for m in get_client(conn).models.list().data]
