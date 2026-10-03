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
