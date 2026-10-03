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
# Каждый шлюз задаётся одним параметром mqtt_base (например "sms/gate-yauheni"),
# все топики — <mqtt_base>/<суффикс>. Та же схема в прошивке (embedded/src/topics.h).
#
# Словари ниже: {топик: entry}, где entry = {name, telegram_bot_token, telegram_chat_id,
# и топики шлюза для публикации: send_topic, cmd_topic, ussd_topic, forward_topic,
# а также availability_topic и status_topic для поиска состояния шлюза}.
GATEWAYS = {}               # <base>/incoming — входящие SMS (PDU)
AVAILABILITY_TOPICS = {}    # <base>/availability — online/offline (LWT, retained)
STATUS_TOPICS = {}          # <base>/status — метрики устройства, в т.ч. состояние сети SIM
SEND_RESULT_TOPICS = {}     # <base>/send-result — результат отправки SMS
REPORT_TOPICS = {}          # <base>/report — отчёты о доставке (SMS-STATUS-REPORT, PDU)
USSD_RESULT_TOPICS = {}     # <base>/ussd-result — ответы на USSD-запросы
FORWARD_RESULT_TOPICS = {}  # <base>/forward-result — состояние переадресации звонков
DEBUG_TOPICS = {}           # <base>/debug — диагностические строки прошивки (только в журнал)
BOTS = []                   # по одному на шлюз: приём команд из Telegram
for gw in _raw["gateways"]:
    if "mqtt_topic" in gw or "mqtt_base" not in gw:
        print(
            f"Gateway {gw.get('name')!r}: задайте mqtt_base (например \"sms/gate-1\"), "
            "старый параметр mqtt_topic больше не поддерживается.",
            file=sys.stderr,
        )
        sys.exit(1)

    base = gw["mqtt_base"].rstrip("/")
    entry = {
        "name": gw["name"],
        "telegram_bot_token": gw["telegram_bot_token"],
        "telegram_chat_id": str(gw["telegram_chat_id"]),
        # топики, в которые бэкенд публикует
        "send_topic": f"{base}/send",
        "cmd_topic": f"{base}/cmd",
        "ussd_topic": f"{base}/ussd",
        "forward_topic": f"{base}/forward",
        # топики шлюза, по которым ищется его последнее состояние
        "availability_topic": f"{base}/availability",
        "status_topic": f"{base}/status",
    }
    GATEWAYS[f"{base}/incoming"] = entry
    AVAILABILITY_TOPICS[entry["availability_topic"]] = entry
    STATUS_TOPICS[entry["status_topic"]] = entry
    SEND_RESULT_TOPICS[f"{base}/send-result"] = entry
    REPORT_TOPICS[f"{base}/report"] = entry
    USSD_RESULT_TOPICS[f"{base}/ussd-result"] = entry
    FORWARD_RESULT_TOPICS[f"{base}/forward-result"] = entry
    DEBUG_TOPICS[f"{base}/debug"] = entry
    BOTS.append(entry)

# ── Outgoing SMS limits ───────────────────────────────
SEND_MAX_PARTS = _raw.get("send_max_parts", 6)             # максимум частей в одном сообщении
SEND_RATE_LIMIT_PER_MIN = _raw.get("send_rate_limit_per_min", 5)  # на шлюз
SEND_RESULT_TIMEOUT_SEC = _raw.get("send_result_timeout_sec", 120)
USSD_TIMEOUT_SEC = _raw.get("ussd_timeout_sec", 60)
FORWARD_TIMEOUT_SEC = _raw.get("forward_timeout_sec", 180)  # до 4 запросов к сети по ~30 с
