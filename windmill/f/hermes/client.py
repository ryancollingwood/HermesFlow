"""
Shared Hermes client for Windmill — path: f/hermes/client

Other scripts reuse the helpers from here:

    from f.hermes.client import hermes_endpoint, chat

`hermes_endpoint` is a Windmill RESOURCE TYPE (base_url + api_key). Create one
resource of this type (e.g. f/hermes/local) and pass it into any script — no
base URL or key hardcoded in the scripts themselves.

Running THIS script directly returns the list of models the gateway serves,
which doubles as a connectivity test.
"""
from typing import TypedDict
from openai import OpenAI


class hermes_endpoint(TypedDict):
    base_url: str
    api_key: str


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
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    )
    return resp.choices[0].message.content


def main(conn: hermes_endpoint) -> list[str]:
    """Connectivity test: list the models the gateway serves."""
    return [m.id for m in get_client(conn).models.list().data]
