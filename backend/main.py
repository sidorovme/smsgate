"""
SMS Gateway Backend
Listens to MQTT, parses PDU, assembles multipart, sends to Telegram.
"""

import json
import logging
import random
import re
import signal
import sys
import threading
import time
from collections import defaultdict, deque

import paho.mqtt.client as mqtt

import config
import db
import pdu_encoder
import pdu_parser
import telegram

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("smsgate")

running = True

# Последнее известное состояние доступности по topic (для детекта переходов)
_availability = {}

# Последнее известное состояние сотовой сети по status-топику: (registered, operator)
_network = {}

_client = None  # MQTT-клиент, нужен обработчикам Telegram-команд для публикации
_send_times = defaultdict(deque)  # gateway name -> времена последних отправок (rate limit)
_send_lock = threading.Lock()
_ref_counter = random.randint(1, 255)  # TP-reference для UDH (multipart)

_status_cache = {}     # status topic -> (время получения, dict последнего статуса шлюза)
_expected_offline = {}  # availability topic -> время команды reboot (подавляем алерт offline)
EXPECTED_REBOOT_SEC = 60

USAGE = "Использование: <code>/send +375291234567 Текст сообщения</code>"
_ussd_pending = {}  # id -> gateway name (ждём ответ шлюза)
_ussd_id = int(time.time()) % 1_000_000_000
USSD_RE = re.compile(r"^[0-9*#+]{1,64}$")

HELP = (
    "/send <code>+375291234567 текст</code> — отправить SMS\n"
    "/ussd <code>*101#</code> — USSD-запрос (<code>/ussd cancel</code> закрывает сессию)\n"
    "/status — состояние шлюза\n"
    "/reboot — перезагрузить шлюз\n"
    "/reset_modem — перезапустить модем"
)


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        for topic in config.GATEWAYS:
            log.info("MQTT subscribing to %s", topic)
            client.subscribe(topic)
        for topic in config.AVAILABILITY_TOPICS:
            log.info("MQTT subscribing to %s", topic)
            client.subscribe(topic)
        for topic in (
            list(config.STATUS_TOPICS)
            + list(config.SEND_RESULT_TOPICS)
            + list(config.REPORT_TOPICS)
            + list(config.DEBUG_TOPICS)
            + list(config.USSD_RESULT_TOPICS)
        ):
            log.info("MQTT subscribing to %s", topic)
            client.subscribe(topic)
    else:
        log.error("MQTT connection failed: %s", reason_code)


def handle_availability(topic, msg):
    """React to a gateway's LWT/birth message: alert Telegram on transitions."""
    gateway = config.AVAILABILITY_TOPICS.get(topic)
    if gateway is None:
        return

    state = msg.payload.decode(errors="replace").strip().lower()
    if state not in ("online", "offline"):
        log.warning("Unknown availability payload on %s: %r", topic, state)
        return

    prev = _availability.get(topic)
    _availability[topic] = state

    if prev == state:
        return  # без изменений (в т.ч. дубликат retained-сообщения)

    if prev is None and state == "online":
        # первое известие о шлюзе, он в сети — это норма, не шумим
        log.info("Gateway %s is online (baseline)", gateway["name"])
        return

    log.info("Gateway %s availability: %s -> %s", gateway["name"], prev, state)
    if state == "offline" and time.time() - _expected_offline.pop(topic, 0) < EXPECTED_REBOOT_SEC:
        return  # плановая перезагрузка по команде из Telegram — не алертим, придёт «снова в сети»
    telegram.send_availability(
        gateway["name"],
        state == "online",
        gateway["telegram_bot_token"],
        gateway["telegram_chat_id"],
    )


def handle_status(topic, msg):
    """Track cellular network registration/operator; alert Telegram on changes."""
    gateway = config.STATUS_TOPICS.get(topic)
    if gateway is None:
        return

    status = json.loads(msg.payload.decode())
    _status_cache[topic] = (time.time(), status)
    if status.get("net_stat", -1) == -1:
        return  # прошивка без поддержки или модем ещё не опрошен

    registered = bool(status.get("net_registered"))
    operator = status.get("net_operator") or ""
    roaming = bool(status.get("net_roaming"))

    prev = _network.get(topic)
    _network[topic] = (registered, operator)

    if prev == (registered, operator):
        return

    if prev is None:
        # первое известие: нормальное состояние не комментируем, проблему — сообщаем
        log.info("Gateway %s network baseline: registered=%s operator=%s",
                 gateway["name"], registered, operator)
        if registered:
            return
    else:
        log.info("Gateway %s network: %s -> %s", gateway["name"], prev, (registered, operator))

    telegram.send_network(
        gateway["name"],
        registered,
        operator,
        roaming,
        prev[1] if prev and prev[0] else None,
        gateway["telegram_bot_token"],
        gateway["telegram_chat_id"],
    )


# ── Outgoing SMS ──────────────────────────────────────

def _reply(gateway, message):
    telegram.send_text(message, gateway["telegram_bot_token"], gateway["telegram_chat_id"])


def _next_reference():
    global _ref_counter
    with _send_lock:
        _ref_counter = _ref_counter % 255 + 1
        return _ref_counter


def _rate_limited(gateway):
    now = time.time()
    with _send_lock:
        times = _send_times[gateway["name"]]
        while times and now - times[0] > 60:
            times.popleft()
        if len(times) >= config.SEND_RATE_LIMIT_PER_MIN:
            return True
        times.append(now)
        return False


def handle_command(gateway, text):
    """Dispatch a Telegram command from the gateway's authorized chat."""
    parts = text.strip().split(None, 2)
    if not parts or not parts[0].startswith("/"):
        return
    cmd = parts[0].split("@")[0].lower()
    if cmd == "/send":
        handle_send_command(gateway, parts)
    elif cmd == "/ussd":
        handle_ussd_command(gateway, parts[1] if len(parts) > 1 else "")
    elif cmd == "/status":
        handle_status_command(gateway)
    elif cmd in ("/reboot", "/reset_modem", "/reset-modem"):
        handle_control_command(gateway, "reboot" if cmd == "/reboot" else "reset-modem")
    elif cmd in ("/help", "/start"):
        _reply(gateway, HELP)


def _fmt_uptime(sec):
    d, rem = divmod(int(sec), 86400)
    h, rem = divmod(rem, 3600)
    return (f"{d}д " if d else "") + f"{h}ч {rem // 60}м"


def _fmt_status(gateway, online, age, st):
    esc = telegram.escape_html
    lines = [f"<b>{esc(gateway['name'])}</b> — {'🟢 online' if online else '🔴 offline'}"]
    if not online:
        lines.append(f"<i>последние известные данные ({_fmt_uptime(age)} назад)</i>")

    if st.get("net_stat", -1) == -1:
        net = "нет данных"
    elif st.get("net_registered"):
        net = esc(st.get("net_operator") or "?") + (" (роуминг)" if st.get("net_roaming") else "")
    else:
        net = "❌ не зарегистрирована"
    lines.append(f"SIM: {net}")

    csq = st.get("net_csq", 99)
    if csq != 99 and st.get("net_registered"):
        lines.append(f"Сигнал: {-113 + 2 * csq} dBm (CSQ {csq}/31)")

    lines.append(f"Модем: {'OK' if st.get('sim900_ok') else '❌ не отвечает'}")
    lines.append(f"Wi-Fi: {st.get('wifi_rssi')} dBm, {esc(str(st.get('wifi_ip', '?')))}")
    lines.append(f"Uptime: {_fmt_uptime(st.get('uptime_sec', 0))}")
    lines.append(
        f"SMS: принято {st.get('sms_forwarded', 0)}, отправлено {st.get('sms_sent', 0)}, "
        f"в очереди на SIM {st.get('sms_pending', 0)}"
    )
    if st.get("last_error"):
        lines.append(f"Последняя ошибка: {esc(str(st['last_error']))}")
    lines.append(f"Heap: {st.get('free_heap', 0) // 1024} КБ")
    return "\n".join(lines)


def handle_status_command(gateway):
    """Ask the gateway for fresh metrics and reply with a summary."""
    online = _availability.get(gateway["availability_topic"]) == "online"
    asked = time.time()
    if online:
        _client.publish(gateway["cmd_topic"], "status")
        deadline = asked + 5
        while time.time() < deadline:
            cached = _status_cache.get(gateway["status_topic"])
            if cached and cached[0] >= asked:
                break
            time.sleep(0.2)

    cached = _status_cache.get(gateway["status_topic"])
    if cached is None:
        _reply(gateway, f"Нет данных о <b>{telegram.escape_html(gateway['name'])}</b>")
        return
    ts, st = cached
    _reply(gateway, _fmt_status(gateway, online, time.time() - ts, st))


def handle_control_command(gateway, command):
    """Forward reboot / reset-modem to the gateway (only if it is online)."""
    name = telegram.escape_html(gateway["name"])
    if _availability.get(gateway["availability_topic"]) != "online":
        _reply(gateway, f"❌ <b>{name}</b> недоступен (offline), команда не отправлена.")
        return

    if command == "reboot":
        _expected_offline[gateway["availability_topic"]] = time.time()
    info = _client.publish(gateway["cmd_topic"], command, qos=1)
    if info.rc != mqtt.MQTT_ERR_SUCCESS:
        _expected_offline.pop(gateway["availability_topic"], None)
        _reply(gateway, "❌ Не удалось передать команду шлюзу (MQTT).")
        return

    log.info("Command %s sent to %s", command, gateway["name"])
    if command == "reboot":
        _reply(gateway, f"🔄 Перезагружаю <b>{name}</b>, сообщу, когда вернётся в сеть.")
    else:
        _reply(gateway, f"🔄 Перезапускаю модем <b>{name}</b> (около 10–15 с).")


def handle_ussd_command(gateway, arg):
    """Handle '/ussd <code>' or '/ussd cancel'."""
    global _ussd_id
    name = telegram.escape_html(gateway["name"])
    cancel = arg.lower() == "cancel"
    if not cancel and not USSD_RE.match(arg):
        _reply(gateway, "Использование: <code>/ussd *101#</code> (допустимы цифры и символы * # +)\n"
                        "Выбор в меню: <code>/ussd 1</code>, закрыть сессию: <code>/ussd cancel</code>")
        return

    if _availability.get(gateway["availability_topic"]) != "online":
        _reply(gateway, f"❌ <b>{name}</b> недоступен (offline). Повторите позже.")
        return
    net = _network.get(gateway["status_topic"])
    if not cancel and net is not None and not net[0]:
        _reply(gateway, f"❌ SIM-карта <b>{name}</b> не в сети. Повторите позже.")
        return
    if _rate_limited(gateway):
        _reply(gateway, f"❌ Превышен лимит: не более {config.SEND_RATE_LIMIT_PER_MIN} запросов в минуту")
        return

    with _send_lock:
        _ussd_id += 1
        req_id = _ussd_id
    payload = {"id": req_id, "cancel": True} if cancel else {"id": req_id, "code": arg}
    info = _client.publish(gateway["ussd_topic"], json.dumps(payload), qos=1)
    if info.rc != mqtt.MQTT_ERR_SUCCESS:
        _reply(gateway, "❌ Не удалось передать команду шлюзу (MQTT).")
        return

    log.info("USSD #%d %s via %s", req_id, "cancel" if cancel else arg, gateway["name"])
    _ussd_pending[req_id] = gateway["name"]
    _reply(gateway, "⏳ Жду ответ оператора…")

    def on_timeout():
        if _ussd_pending.pop(req_id, None) is not None:
            _reply(gateway, f"❌ Нет ответа от <b>{name}</b> на USSD-запрос.")

    timer = threading.Timer(config.USSD_TIMEOUT_SEC, on_timeout)
    timer.daemon = True
    timer.start()


def _decode_ussd(text, dcs):
    """USSD text from the modem: UCS-2 answers (dcs 72 / 0x08 family) arrive as hex."""
    if (dcs & 0x0C) == 0x08 or dcs == 72:
        try:
            return bytes.fromhex(text).decode("utf-16-be", errors="replace")
        except ValueError:
            pass
    return text


def handle_ussd_result(topic, msg):
    gateway = config.USSD_RESULT_TOPICS.get(topic)
    if gateway is None:
        return

    result = json.loads(msg.payload.decode())
    req_id = int(result["id"])
    if _ussd_pending.pop(req_id, None) is None:
        return  # поздний ответ после таймаута или чужой

    if result.get("status") != "ok":
        error = telegram.escape_html(str(result.get("error", "неизвестная ошибка")))
        _reply(gateway, f"❌ USSD: {error}")
        return

    n = int(result.get("n", 0))
    text = _decode_ussd(result.get("text", ""), int(result.get("dcs", 0)))
    log.info("USSD #%d result n=%d (%d chars)", req_id, n, len(text))

    body = telegram.escape_html(text) if text else "<i>(пустой ответ)</i>"
    if n == 1:
        body += "\n\n<i>Ожидается ответ: <code>/ussd &lt;выбор&gt;</code>, закрыть: <code>/ussd cancel</code></i>"
    elif n == 2:
        body += "\n\n<i>Сессия завершена оператором</i>"
    elif n == 4:
        body += "\n\n<i>Операция не поддерживается</i>"
    _reply(gateway, "📟 " + body)


def handle_send_command(gateway, parts):
    """Handle '/send <number> <text>' (parts = text.split(None, 2))."""
    if len(parts) < 3:
        _reply(gateway, USAGE)
        return

    try:
        number = pdu_encoder.normalize_number(parts[1])
        pdus = pdu_encoder.encode(number, parts[2], _next_reference())
    except pdu_encoder.EncodeError as e:
        _reply(gateway, f"❌ {telegram.escape_html(str(e))}\n{USAGE}")
        return

    name = telegram.escape_html(gateway["name"])
    if len(pdus) > config.SEND_MAX_PARTS:
        _reply(gateway, f"❌ Слишком длинное сообщение: {len(pdus)} частей "
                        f"(максимум {config.SEND_MAX_PARTS})")
        return

    # Шлюз офлайн / SIM не в сети — отклоняем сразу, пользователь повторит позже
    if _availability.get(gateway["availability_topic"]) != "online":
        _reply(gateway, f"❌ <b>{name}</b> недоступен (offline), SMS не отправлено. Повторите позже.")
        return
    net = _network.get(gateway["status_topic"])
    if net is not None and not net[0]:
        _reply(gateway, f"❌ SIM-карта <b>{name}</b> не в сети, SMS не отправлено. Повторите позже.")
        return

    if _rate_limited(gateway):
        _reply(gateway, f"❌ Превышен лимит: не более {config.SEND_RATE_LIMIT_PER_MIN} SMS в минуту")
        return

    msg_id = db.create_sent(gateway["name"], number, parts[2], len(pdus))
    payload = {
        "id": msg_id,
        "to": number,
        "parts": [{"pdu": pdu, "len": length} for pdu, length in pdus],
    }
    info = _client.publish(gateway["send_topic"], json.dumps(payload), qos=1)
    if info.rc != mqtt.MQTT_ERR_SUCCESS:
        db.fail_pending(msg_id)
        _reply(gateway, "❌ Не удалось передать команду шлюзу (MQTT). Повторите позже.")
        return

    log.info("Send #%d to %s via %s (%d parts)", msg_id, number, gateway["name"], len(pdus))
    _reply(gateway, f"⏳ Отправляю на <b>{telegram.escape_html(number)}</b>"
                    + (f" ({len(pdus)} ч.)" if len(pdus) > 1 else ""))

    def on_timeout():
        if db.is_pending(msg_id):
            db.fail_pending(msg_id)
            log.warning("Send #%d: no result from gateway", msg_id)
            _reply(gateway, f"❌ Нет ответа от шлюза <b>{name}</b>, статус отправки на "
                            f"{telegram.escape_html(number)} неизвестен.")

    timer = threading.Timer(config.SEND_RESULT_TIMEOUT_SEC, on_timeout)
    timer.daemon = True
    timer.start()


def handle_send_result(topic, msg):
    """Gateway reports the outcome of a send job."""
    gateway = config.SEND_RESULT_TOPICS.get(topic)
    if gateway is None:
        return

    result = json.loads(msg.payload.decode())
    msg_id = int(result["id"])
    ok = result.get("status") == "ok"
    refs = [int(r) for r in result.get("refs", [])]

    row = db.apply_send_result(msg_id, ok, refs)
    if row is None:
        log.info("Send result for unknown/finished #%s ignored", msg_id)
        return

    to = telegram.escape_html(row["recipient"])
    log.info("Send #%d result: %s %s", msg_id, result.get("status"), result.get("error", ""))
    if ok:
        _reply(gateway, f"📤 Отправлено на <b>{to}</b>, жду отчёт о доставке")
    else:
        error = telegram.escape_html(str(result.get("error", "неизвестная ошибка")))
        extra = f" (отправлено частей: {len(refs)} из {row['parts_total']})" if refs else ""
        _reply(gateway, f"❌ Не отправлено на <b>{to}</b>: {error}{extra}")


def handle_report(topic, msg):
    """Delivery report (SMS-STATUS-REPORT) forwarded by the gateway."""
    gateway = config.REPORT_TOPICS.get(topic)
    if gateway is None:
        return

    payload = json.loads(msg.payload.decode())
    report = pdu_parser.parse_status_report(payload.get("pdu", ""))
    log.info("Delivery report from %s: %s", gateway["name"], report)

    if report["state"] == "pending":
        return  # временная ошибка, оператор ещё пытается — дождёмся финального отчёта

    outcome = db.apply_delivery_report(
        gateway["name"], report["reference"], report["recipient"], report["state"]
    )
    if outcome is None:
        return

    row, final = outcome
    to = telegram.escape_html(row["recipient"])
    if final == "delivered":
        _reply(gateway, f"✅ Доставлено: <b>{to}</b>")
    else:
        _reply(gateway, f"⚠️ Не доставлено: <b>{to}</b> (код оператора 0x{report['status']:02X})")


def on_message(client, userdata, msg):
    try:
        topic = msg.topic

        if topic in config.STATUS_TOPICS:
            handle_status(topic, msg)
            return

        if topic in config.USSD_RESULT_TOPICS:
            handle_ussd_result(topic, msg)
            return

        if topic in config.DEBUG_TOPICS:
            log.info("[%s] DBG %s", config.DEBUG_TOPICS[topic]["name"],
                     msg.payload.decode(errors="replace"))
            return

        if topic in config.SEND_RESULT_TOPICS:
            handle_send_result(topic, msg)
            return

        if topic in config.REPORT_TOPICS:
            handle_report(topic, msg)
            return

        if topic in config.AVAILABILITY_TOPICS:
            handle_availability(topic, msg)
            return

        gateway = config.GATEWAYS.get(topic)
        if gateway is None:
            log.warning("Message from unknown topic: %s", topic)
            return

        bot_token = gateway["telegram_bot_token"]
        chat_id = gateway["telegram_chat_id"]

        payload = json.loads(msg.payload.decode())
        pdu_hex = payload.get("pdu", "")
        device_id = payload.get("device_id", "unknown")

        if not pdu_hex:
            log.warning("Empty PDU received on %s", topic)
            return

        log.info("Received PDU from %s on %s (%d chars)", device_id, topic, len(pdu_hex))

        # Parse PDU
        parsed = pdu_parser.parse(pdu_hex)
        sender = parsed["sender"]
        text = parsed["text"]
        timestamp = parsed["timestamp"]
        multipart = parsed["multipart"]

        if multipart is None:
            # Single SMS — save and send immediately
            db.save_single(device_id, sender, text, timestamp)
            telegram.send(device_id, sender, timestamp, text, bot_token, chat_id)
        else:
            # Multipart — save part, try to assemble
            ref = multipart["reference"]
            total = multipart["total"]
            part = multipart["part"]

            log.info(
                "Multipart %d/%d (ref=%d) from %s",
                part, total, ref, sender,
            )

            db.save_part(device_id, sender, ref, total, part, text)

            full_text = db.try_assemble(device_id, sender, ref, total, timestamp)
            if full_text is not None:
                telegram.send(device_id, sender, timestamp, full_text, bot_token, chat_id)

    except Exception:
        log.exception("Error processing message")


def cleanup_loop():
    """Periodically clean up stale multipart parts."""
    while running:
        time.sleep(60)
        try:
            db.cleanup_stale()
        except Exception:
            log.exception("Cleanup error")


def main():
    global running, _client

    log.info("=== SMS Gateway Backend starting ===")

    # Initialize database
    db.init()

    # MQTT client
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="smsgate-backend",
    )
    client.on_connect = on_connect
    client.on_message = on_message

    _client = client

    client.connect(config.MQTT_BROKER, config.MQTT_PORT, keepalive=60)

    # Cleanup thread
    cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
    cleanup_thread.start()

    # Меню команд бота — только в чате своего шлюза
    for gw in config.BOTS:
        telegram.set_commands(gw["name"], gw["telegram_bot_token"], gw["telegram_chat_id"])

    # Telegram: приём команд /send. Один поток на токен бота (getUpdates — один потребитель
    # на бота), чаты шлюзов с общим токеном обслуживаются одним потоком.
    by_token = {}
    for gw in config.BOTS:
        by_token.setdefault(gw["telegram_bot_token"], {})[gw["telegram_chat_id"]] = (
            lambda text, gw=gw: handle_command(gw, text)
        )
    for token, handlers in by_token.items():
        label = ",".join(gw["name"] for gw in config.BOTS if gw["telegram_bot_token"] == token)
        threading.Thread(
            target=telegram.poll_commands,
            args=(label, token, handlers, lambda: running),
            daemon=True,
        ).start()

    # Graceful shutdown
    def shutdown(signum, frame):
        global running
        log.info("Shutting down...")
        running = False
        client.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    log.info("=== SMS Gateway Backend ready ===")
    client.loop_forever()


if __name__ == "__main__":
    main()
