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

# ── Gateways: {mqtt_topic: {name, telegram_bot_token, telegram_chat_id}} ──
GATEWAYS = {}
for gw in _raw["gateways"]:
    GATEWAYS[gw["mqtt_topic"]] = {
        "name": gw["name"],
        "telegram_bot_token": gw["telegram_bot_token"],
        "telegram_chat_id": str(gw["telegram_chat_id"]),
    }
