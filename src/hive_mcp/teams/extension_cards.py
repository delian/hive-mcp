"""The JSON the message extension answers with: result cards, dialogs, plain messages."""

from __future__ import annotations

import re
from typing import Any

HERO = "application/vnd.microsoft.card.hero"
ADAPTIVE = "application/vnd.microsoft.card.adaptive"
MARKDOWN = re.compile(r"([\\`*_{}\[\]()#+\-.!|~<>])")
PRIVATE = "private"  # the dropdown's value for "keep it to myself"


def plain(text: str) -> str:
    """Stored text as a card shows it, escaped: no link, mention or formatting can come out."""
    return MARKDOWN.sub(r"\\\1", " ".join(text.split()))


def message(text: str) -> dict[str, Any]:
    return {"composeExtension": {"type": "message", "text": text}}


def task_message(text: str) -> dict[str, Any]:
    return {"task": {"type": "message", "value": text}}


def card(title: str, subtitle: str, text: str, url: str | None = None) -> dict[str, Any]:
    """A hero card with a preview. The preview may not carry buttons, the card itself may."""
    content: dict[str, Any] = {
        "title": plain(title)[:200],
        "subtitle": plain(subtitle)[:100],
        "text": text,
    }
    shown = (
        {**content, "buttons": [{"type": "openUrl", "title": "Open in Hive", "value": url}]}
        if url
        else content
    )
    return {
        "contentType": HERO,
        "content": shown,
        "preview": {"contentType": HERO, "content": content},
    }


def result(cards: list[dict[str, Any]], *, fresh: bool = False) -> dict[str, Any]:
    """A list of cards. `fresh`: Teams must not cache it (what it shows depends on who asks)."""
    body: dict[str, Any] = {"type": "result", "attachmentLayout": "list", "attachments": cards}
    if fresh:
        body["suggestedActions"] = {
            "actions": [{"type": "setCachePolicy", "value": '{"type":"no-cache"}'}]
        }
    return {"composeExtension": body}


def save_dialog(token: str, preview: str, projects: list[str]) -> dict[str, Any]:
    """Where to keep a message: private, or one of the person's projects."""
    choices = [{"title": "Private to me", "value": PRIVATE}] + [
        {"title": f"Project {p}", "value": p} for p in projects
    ]
    content = {
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
            {"type": "TextBlock", "text": plain(preview)[:160], "wrap": True, "isSubtle": True},
            {
                "type": "Input.ChoiceSet",
                "id": "project",
                "label": "Keep it",
                "style": "compact",
                "value": PRIVATE,
                "choices": choices,
            },
        ],
        "actions": [{"type": "Action.Submit", "title": "Save", "data": {"token": token}}],
    }
    return {
        "task": {
            "type": "continue",
            "value": {
                "title": "Save to Hive",
                "height": "small",
                "width": "medium",
                "card": {"contentType": ADAPTIVE, "content": content},
            },
        }
    }
