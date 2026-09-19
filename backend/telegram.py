"""
Telegram Bot API integration — send SMS notifications.
"""

import logging

import requests

log = logging.getLogger(__name__)


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _post(bot_token: str, chat_id: str, message: str) -> bool:
    """Send an HTML message via the Telegram Bot API. Returns True on success."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    try:
        resp = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
            },
            timeout=10,
        )

        if resp.ok:
            return True
        else:
            log.error("Telegram API error %d: %s", resp.status_code, resp.text)
            return False

    except Exception:
        log.exception("Telegram send failed")
        return False


def send(
    device_id: str, sender: str, timestamp: str, text: str, bot_token: str, chat_id: str
) -> bool:
    """
    Send SMS notification to Telegram.
    Returns True if sent successfully.
    """
    message = (
        f"{_escape_html(text)}\n\n"
        f"<b>From:</b> {_escape_html(sender)}\n"
        f"<b>At:</b> {_escape_html(timestamp)}\n"
        f"<b>Via:</b> {_escape_html(device_id)}\n"
    )

    if _post(bot_token, chat_id, message):
        log.info("Telegram sent: SMS from %s", sender)
        return True
    return False


def send_availability(name: str, online: bool, bot_token: str, chat_id: str) -> bool:
    """Notify Telegram that a gateway went online/offline. Returns True on success."""
    if online:
        message = f"✅ <b>{_escape_html(name)}</b> снова в сети"
    else:
        message = f"⚠️ <b>{_escape_html(name)}</b> недоступен (offline)"

    if _post(bot_token, chat_id, message):
        log.info("Telegram alert: %s -> %s", name, "online" if online else "offline")
        return True
    return False
