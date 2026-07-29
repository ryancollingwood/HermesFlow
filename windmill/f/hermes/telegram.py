"""
Shared Telegram document delivery for Windmill — path: f/hermes/telegram

Other scripts reuse this via:

    from f.hermes.telegram import deliver_document_to_telegram

Reads the bot token and the comma-separated allow-list from the
f/hermes/telegram_bot_token and f/hermes/telegram_allow_user secrets. Both are
seeded from TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOWED_USERS in .env by
`make windmill-push` / install.sh / install.py — see README.md
"Windmill ⇄ Hermes integration".

Running THIS script directly sends a test document to every allow-listed user.
"""
import requests
import wmill

TELEGRAM_API = "https://api.telegram.org"


def _allowed_chat_ids() -> list[str]:
    raw = wmill.get_variable("f/hermes/telegram_allow_user")
    return [chat_id.strip() for chat_id in raw.split(",") if chat_id.strip()]


def deliver_document_to_telegram(document_text: str, document_name: str = "report.md") -> list[dict]:
    """Send `document_text` as a file to every Telegram user in the allow-list.

    Returns one {chat_id, status, error} dict per recipient — a bad chat_id
    doesn't stop delivery to the rest of the allow-list.
    """
    token = wmill.get_variable("f/hermes/telegram_bot_token")
    payload = document_text.encode()
    results = []
    for chat_id in _allowed_chat_ids():
        try:
            resp = requests.post(
                f"{TELEGRAM_API}/bot{token}/sendDocument",
                data={"chat_id": chat_id},
                files={"document": (document_name, payload, "text/markdown")},
                timeout=30,
            )
            resp.raise_for_status()
            results.append({"chat_id": chat_id, "status": "sent", "error": None})
        except requests.RequestException as exc:
            results.append({"chat_id": chat_id, "status": "failed", "error": str(exc)})
    return results


def main(document_text: str, document_name: str = "report.md") -> list[dict]:
    return deliver_document_to_telegram(document_text, document_name)
