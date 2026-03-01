"""
SMS Gateway Backend
Listens to MQTT, parses PDU, assembles multipart, sends to Telegram.
"""

import json
import logging
import signal
import sys
import threading
import time

import paho.mqtt.client as mqtt

import config
import db
import pdu_parser
import telegram

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("smsgate")

running = True


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        for topic in config.GATEWAYS:
            log.info("MQTT subscribing to %s", topic)
            client.subscribe(topic)
    else:
        log.error("MQTT connection failed: %s", reason_code)


def on_message(client, userdata, msg):
    try:
        topic = msg.topic
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
    global running

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

    client.connect(config.MQTT_BROKER, config.MQTT_PORT, keepalive=60)

    # Cleanup thread
    cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
    cleanup_thread.start()

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
