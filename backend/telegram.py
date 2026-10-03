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


def _call(bot_token: str, method: str, payload: dict):
    """Call a Bot API method. Returns the parsed response dict, or None on failure."""
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/{method}", json=payload, timeout=10
        )
        if resp.ok:
            return resp.json()
        log.error("Telegram %s error %d: %s", method, resp.status_code, resp.text)
    except Exception as e:
        log.error("Telegram %s failed: %s", method, _safe_error(e))
    return None


def _post(bot_token: str, chat_id: str, message: str) -> bool:
    """Send an HTML message via the Telegram Bot API. Returns True on success."""
    return send_message(bot_token, chat_id, message) is not None


def send_message(bot_token: str, chat_id: str, message: str):
    """Send an HTML message. Returns its message_id, or None on failure."""
    data = _call(
        bot_token, "sendMessage", {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
    )
    return data["result"]["message_id"] if data else None


def edit_message(bot_token: str, chat_id: str, message_id: int, message: str) -> bool:
    """Replace the text of an already sent message. 'Not modified' counts as success."""
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/editMessageText",
            json={
                "chat_id": chat_id,
                "message_id": message_id,
                "text": message,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        if resp.ok or "message is not modified" in resp.text:
            return True
        log.error("Telegram editMessageText error %d: %s", resp.status_code, resp.text)
    except Exception as e:
        log.error("Telegram editMessageText failed: %s", _safe_error(e))
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


def poll_commands(name: str, bot_token: str, handlers: dict, is_running):
    """
    Long-poll a bot and dispatch text messages to handlers[chat_id](text).
    Messages from chats without a handler are ignored. Backlog accumulated while the
    backend was down is discarded, so a stale /send is never executed late.
    One poller per token: Telegram allows only one getUpdates consumer per bot (409 otherwise).
    """
    offset = None
    try:
        backlog = _get_updates(bot_token, -1, 0)
        if backlog:
            offset = backlog[-1]["update_id"] + 1
    except Exception as e:
        log.error("[%s] Telegram: failed to skip backlog: %s", name, _safe_error(e))

    while is_running():
        try:
            for upd in _get_updates(bot_token, offset, 30):
                offset = upd["update_id"] + 1
                msg = upd.get("message")
                if not msg or "text" not in msg:
                    continue
                handler = handlers.get(str(msg["chat"]["id"]))
                if handler is None:
                    log.warning("[%s] Telegram: ignoring message from chat %s", name, msg["chat"]["id"])
                    continue
                if time.time() - msg.get("date", 0) > 120:
                    continue  # устаревшая команда
                try:
                    handler(msg["text"])
                except Exception:
                    log.exception("[%s] Telegram command handler failed", name)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 409:
                log.error("[%s] Telegram 409: another getUpdates consumer or webhook uses this "
                          "bot token (second backend instance? webhook set?)", name)
                time.sleep(30)
            else:
                log.error("[%s] Telegram polling error: %s", name, _safe_error(e))
                time.sleep(5)
        except Exception as e:
            log.error("[%s] Telegram polling error: %s", name, _safe_error(e))
            time.sleep(5)


def _safe_error(e: Exception) -> str:
    """Error text without the request URL (it contains the bot token)."""
    resp = getattr(e, "response", None)
    if resp is not None:
        return f"HTTP {resp.status_code}"
    return type(e).__name__


BOT_COMMANDS = [
    {"command": "status", "description": "Состояние шлюза"},
    {"command": "ussd", "description": "USSD-запрос: /ussd *101#"},
    {"command": "forward", "description": "Переадресация звонков: /forward +995... | off"},
    {"command": "send", "description": "Отправить SMS: /send +375... текст"},
    {"command": "reboot", "description": "Перезагрузить шлюз"},
    {"command": "reset_modem", "description": "Перезапустить модем"},
    {"command": "help", "description": "Список команд"},
]


def set_commands(name: str, bot_token: str, chat_id: str) -> bool:
    """Register the bot command menu for the gateway's chat only (setMyCommands, chat scope)."""
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/setMyCommands",
            json={
                "commands": BOT_COMMANDS,
                "scope": {"type": "chat", "chat_id": chat_id},
            },
            timeout=10,
        )
        if resp.ok:
            log.info("[%s] Telegram command menu registered", name)
            return True
        log.error("[%s] setMyCommands failed: HTTP %d %s", name, resp.status_code, resp.text)
    except Exception as e:
        log.error("[%s] setMyCommands failed: %s", name, _safe_error(e))
    return False
