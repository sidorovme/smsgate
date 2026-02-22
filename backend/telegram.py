"""
Telegram Bot API integration — send SMS notifications.
"""

import logging

import requests

import config

log = logging.getLogger(__name__)


def _escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def send(device_id: str, sender: str, timestamp: str, text: str) -> bool:
    """
    Send SMS notification to Telegram.
    Returns True if sent successfully.
    """
    message = (
        f"<b>📩 New SMS Alert</b>\n\n"
        f"<b>SIM:</b> {_escape_html(device_id)}\n"
        f"<b>Sender:</b> {_escape_html(sender)}\n"
        f"<b>Time:</b> {_escape_html(timestamp)}\n\n"
        f"{_escape_html(text)}"
    )

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"

    try:
        resp = requests.post(
            url,
            json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
            },
            timeout=10,
        )

        if resp.ok:
            log.info("Telegram sent: SMS from %s", sender)
            return True
        else:
            log.error("Telegram API error %d: %s", resp.status_code, resp.text)
            return False

    except Exception:
        log.exception("Telegram send failed")
        return False
