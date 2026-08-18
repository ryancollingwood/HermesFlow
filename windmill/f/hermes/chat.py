"""
Example consumer of the shared Hermes client — path: f/hermes/chat

Pass the `hermes` resource (type: hermes_endpoint). Because the base_url and
api_key live in the resource, this script stays free of any connection details.

If the Windmill form doesn't render a resource picker for `hermes`, define the
TypedDict locally instead of importing it (Windmill matches resource types by
the annotation's name):

    from typing import TypedDict
    class hermes_endpoint(TypedDict):
        base_url: str
        api_key: str
"""
from f.hermes.client import chat, hermes_endpoint


def main(
    hermes: hermes_endpoint,
    prompt: str = "Give me one sentence on why sunscreen is beneficial.",
    model: str = "hermes",
    system: str = "You are a concise assistant.",
) -> str:
    return chat(hermes, prompt, model=model, system=system)
