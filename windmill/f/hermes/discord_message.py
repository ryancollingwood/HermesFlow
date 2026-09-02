"""
Shared Discord document delivery for Windmill — path: f/hermes/discord

Other scripts reuse this via:

    from f.hermes.discord import deliver_document_to_discord_home
    from f.hermes.discord import deliver_markdown_to_discord

Reads the bot token and the comma-separated allow-list from the
f/hermes/discord_bot_token and f/hermes/discord_home_channel secrets. Both are
seeded from DISCORD_BOT_TOKEN / DISCORD_HOME_CHANNEL in .env.

"""

import requests
import wmill

DISCORD_API = "https://discord.com/api/v10"


def _discord_home_channel() -> list[str]:
    raw = wmill.get_variable("f/hermes/discord_home_channel")
    return [channel_id.strip() for channel_id in raw.split(",") if channel_id.strip()]


def _escape_doc_text(text: str) -> str:
    if not isinstance(text, str):
        return ""
    return text.replace("\\n", "\n")


def deliver_document_to_discord(
    channels: list[str], document_text: str, document_name: str = "report.md"
) -> list[dict]:
    """Send `document_text` as a file to every Discord channel in the allow-list.

    Returns one {channel_id, status, error} dict per recipient — a bad channel_id
    doesn't stop delivery to the rest of the allow-list.
    """
    token = wmill.get_variable("f/hermes/discord_bot_token")
    payload = _escape_doc_text(document_text).encode()
    results = []

    # Discord requires the token in the headers
    headers = {
        "Authorization": f"Bot {token}"
        # Let 'requests' automatically set the multipart/form-data Content-Type boundary
    }

    for channel_id in channels:
        try:
            resp = requests.post(
                f"{DISCORD_API}/channels/{channel_id}/messages",
                headers=headers,
                files={"files[0]": (document_name, payload, "text/markdown")},
                timeout=30,
            )
            resp.raise_for_status()
            results.append({"channel_id": channel_id, "status": "sent", "error": None})
        except requests.RequestException as exc:
            results.append(
                {"channel_id": channel_id, "status": "failed", "error": str(exc)}
            )

    return results


def deliver_document_to_discord_home(
    document_text: str, document_name: str = "report.md"
) -> list[dict]:
    channels = _discord_home_channel()
    return deliver_document_to_discord(
        channels=channels,
        document_text=document_text,
        document_name=document_name,
    )


def deliver_markdown_to_discord(
    channels: list[str],
    message_text: str,
    title: str = "Automated Report",
    card_color: int = 0x5865F2,
    footer_message: str = "Delivered via Windmill",
) -> list[dict]:
    """Send `message_text` as a rich Discord Embed to every allow-listed channel."""
    token = wmill.get_variable("f/hermes/discord_bot_token")
    results = []

    headers = {
        "Authorization": f"Bot {token}",
        # Explicitly declare JSON since we are no longer uploading a file
        "Content-Type": "application/json",
    }
    # Handle double escaped characters here
    clean_text = _escape_doc_text(message_text)

    # if this is too long deliver as a document
    if len(clean_text) > 4096:
        return deliver_document_to_discord(
            channels=channels, document_text=clean_text, document_name=title
        )

    # Construct the rich embed payload
    payload = {
        "embeds": [
            {
                "title": title,
                "description": clean_text,  # Discord Markdown (bold, links, code blocks) is supported here
                "color": card_color,  # Integer hex color code (e.g., Discord Blurple)
                "footer": {"text": footer_message},
            }
        ]
    }

    for channel_id in channels:
        try:
            resp = requests.post(
                f"{DISCORD_API}/channels/{channel_id}/messages",
                headers=headers,
                json=payload,  # Send as JSON
                timeout=30,
            )
            resp.raise_for_status()
            results.append({"channel_id": channel_id, "status": "sent", "error": None})
        except requests.RequestException as exc:
            results.append(
                {"channel_id": channel_id, "status": "failed", "error": str(exc)}
            )

    return results


def deliver_markdown_to_discord_home(
    message_text: str,
    title: str = "Automated Report",
    card_color: int = 0x5865F2,
    footer_message: str = "Delivered via Windmill",
) -> list[dict]:
    channels = _discord_home_channel()
    return deliver_markdown_to_discord(
        channels=channels,
        message_text=message_text,
        title=title,
        card_color=card_color,
        footer_message=footer_message,
    )


def main(
    message_text: str,
    title: str = "Automated Report",
    card_color: int = 0x5865F2,
    footer_message: str = "Delivered via Windmill Debug UI",
) -> list[dict]:
    return deliver_markdown_to_discord_home(
        message_text=message_text,
        title=title,
        card_color=card_color,
        footer_message=footer_message,
    )
