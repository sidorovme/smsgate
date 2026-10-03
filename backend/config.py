"""
Configuration loader — reads gateways.yaml and exposes settings.
"""

import os
import sys

import yaml


def _load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


_CONFIG_PATH = os.environ.get("SMSGATE_CONFIG", "gateways.yaml")

try:
    _raw = _load(_CONFIG_PATH)
except FileNotFoundError:
    print(f"Config file not found: {_CONFIG_PATH}", file=sys.stderr)
    print("Copy gateways.yaml.example to gateways.yaml and fill in your values.", file=sys.stderr)
    sys.exit(1)

# ── MQTT ──────────────────────────────────────────────
MQTT_BROKER = _raw["mqtt"]["broker"]
MQTT_PORT = _raw["mqtt"].get("port", 1883)

# ── Database ─────────────────────────────────────────
DB_PATH = _raw.get("db_path", "sms.db")

# ── Multipart ────────────────────────────────────────
MULTIPART_TIMEOUT_SEC = _raw.get("multipart_timeout_sec", 300)

# ── Gateways ──────────────────────────────────────────
# GATEWAYS:            {mqtt_topic:        {name, telegram_bot_token, telegram_chat_id}}
# AVAILABILITY_TOPICS: {availability_topic: {name, telegram_bot_token, telegram_chat_id}}
#
# availability_topic по умолчанию выводится из mqtt_topic
# (sms/incoming/<x> → sms/status/<x>/availability), но может быть задан явно.
GATEWAYS = {}
AVAILABILITY_TOPICS = {}
STATUS_TOPICS = {}  # sms/status/<x> — метрики устройства, в т.ч. состояние сотовой сети
SEND_RESULT_TOPICS = {}  # sms/send-result/<x> — результат отправки SMS шлюзом
REPORT_TOPICS = {}       # sms/report/<x> — отчёты о доставке (SMS-STATUS-REPORT, PDU)
USSD_RESULT_TOPICS = {}  # sms/ussd-result/<x> — ответы на USSD-запросы
DEBUG_TOPICS = {}        # sms/debug/<x> — диагностические строки прошивки (только в журнал)
BOTS = []                # по одному на шлюз: для приёма команд /send из Telegram
for gw in _raw["gateways"]:
    entry = {
        "name": gw["name"],
        "telegram_bot_token": gw["telegram_bot_token"],
        "telegram_chat_id": str(gw["telegram_chat_id"]),
    }
    mqtt_topic = gw["mqtt_topic"]
    availability_topic = gw.get("availability_topic") or (
        mqtt_topic.replace("/incoming/", "/status/") + "/availability"
    )
    status_topic = gw.get("status_topic") or mqtt_topic.replace("/incoming/", "/status/")
    entry["send_topic"] = gw.get("send_topic") or mqtt_topic.replace("/incoming/", "/send/")
    entry["availability_topic"] = availability_topic
    entry["status_topic"] = status_topic
    entry["cmd_topic"] = gw.get("cmd_topic") or mqtt_topic.replace("/incoming/", "/cmd/")
    GATEWAYS[mqtt_topic] = entry
    STATUS_TOPICS[status_topic] = entry
    SEND_RESULT_TOPICS[
        gw.get("send_result_topic") or mqtt_topic.replace("/incoming/", "/send-result/")
    ] = entry
    REPORT_TOPICS[gw.get("report_topic") or mqtt_topic.replace("/incoming/", "/report/")] = entry
    entry["ussd_topic"] = gw.get("ussd_topic") or mqtt_topic.replace("/incoming/", "/ussd/")
    USSD_RESULT_TOPICS[
        gw.get("ussd_result_topic") or mqtt_topic.replace("/incoming/", "/ussd-result/")
    ] = entry
    DEBUG_TOPICS[gw.get("debug_topic") or mqtt_topic.replace("/incoming/", "/debug/")] = entry
    BOTS.append(entry)
    AVAILABILITY_TOPICS[availability_topic] = entry

# ── Outgoing SMS limits ───────────────────────────────
SEND_MAX_PARTS = _raw.get("send_max_parts", 6)             # максимум частей в одном сообщении
SEND_RATE_LIMIT_PER_MIN = _raw.get("send_rate_limit_per_min", 5)  # на шлюз
SEND_RESULT_TIMEOUT_SEC = _raw.get("send_result_timeout_sec", 120)
USSD_TIMEOUT_SEC = _raw.get("ussd_timeout_sec", 60)
