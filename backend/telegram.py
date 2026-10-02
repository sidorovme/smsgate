"""
Telegram Bot API integration — send SMS notifications.
"""

import logging
import time

import requests

log = logging.getLogger(__name__)


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


escape_html = _escape_html


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


def send_network(
    name: str,
    registered: bool,
    operator: str,
    roaming: bool,
    prev_operator,
    bot_token: str,
    chat_id: str,
) -> bool:
    """Notify Telegram about SIM network registration changes. Returns True on success."""
    n = _escape_html(name)
    op = _escape_html(operator) if operator else "неизвестный оператор"
    if not registered:
        message = f"📵 <b>{n}</b>: SIM-карта отключена от сети"
    elif prev_operator is not None and prev_operator != operator:
        message = (
            f"🔄 <b>{n}</b>: смена оператора "
            f"{_escape_html(prev_operator) or '?'} → {op}"
        )
    else:
        message = f"📶 <b>{n}</b>: SIM-карта в сети — {op}"
    if registered and roaming:
        message += " (роуминг)"

    if _post(bot_token, chat_id, message):
        log.info("Telegram alert: %s network registered=%s operator=%s", name, registered, operator)
        return True
    return False


def send_text(message: str, bot_token: str, chat_id: str) -> bool:
    """Send a ready HTML message (callers escape user-provided parts)."""
    return _post(bot_token, chat_id, message)


def _get_updates(bot_token: str, offset, timeout: int):
    resp = requests.get(
        f"https://api.telegram.org/bot{bot_token}/getUpdates",
        params={"offset": offset, "timeout": timeout, "allowed_updates": '["message"]'},
        timeout=timeout + 10,
    )
    resp.raise_for_status()
    return resp.json().get("result", [])


def poll_commands(name: str, bot_token: str, chat_id: str, handler, is_running):
    """
    Long-poll a bot and call handler(text) for each text message from the authorized chat.
    Messages from other chats are ignored. Backlog accumulated while the backend was down
    is discarded, so a stale /send is never executed late.
    """
    offset = None
    try:
        backlog = _get_updates(bot_token, -1, 0)
        if backlog:
            offset = backlog[-1]["update_id"] + 1
    except Exception:
        log.exception("[%s] Telegram: failed to skip backlog", name)

    while is_running():
        try:
            for upd in _get_updates(bot_token, offset, 30):
                offset = upd["update_id"] + 1
                msg = upd.get("message")
                if not msg or "text" not in msg:
                    continue
                if str(msg["chat"]["id"]) != chat_id:
                    log.warning("[%s] Telegram: ignoring message from chat %s", name, msg["chat"]["id"])
                    continue
                if time.time() - msg.get("date", 0) > 120:
                    continue  # устаревшая команда
                try:
                    handler(msg["text"])
                except Exception:
                    log.exception("[%s] Telegram command handler failed", name)
        except Exception:
            log.exception("[%s] Telegram polling error", name)
            time.sleep(5)
