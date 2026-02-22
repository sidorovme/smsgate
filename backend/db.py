"""
SQLite storage for SMS parts and assembled messages.
"""

import sqlite3
import time
import logging

import config

log = logging.getLogger(__name__)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init():
    """Create tables if they don't exist."""
    conn = _connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sms_parts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id    TEXT    NOT NULL,
            sender       TEXT    NOT NULL,
            reference    INTEGER NOT NULL,
            total_parts  INTEGER NOT NULL,
            part_number  INTEGER NOT NULL,
            text         TEXT    NOT NULL,
            received_at  REAL    NOT NULL,
            UNIQUE(device_id, sender, reference, part_number)
        );

        CREATE TABLE IF NOT EXISTS sms_messages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id       TEXT    NOT NULL,
            sender          TEXT    NOT NULL,
            text            TEXT    NOT NULL,
            timestamp       TEXT    NOT NULL DEFAULT '',
            received_at     REAL    NOT NULL,
            sent_to_telegram INTEGER NOT NULL DEFAULT 0
        );
    """)
    conn.commit()
    conn.close()
    log.info("Database initialized: %s", config.DB_PATH)


def save_single(device_id: str, sender: str, text: str, timestamp: str):
    """Save a single (non-multipart) SMS directly to sms_messages."""
    conn = _connect()
    conn.execute(
        """INSERT INTO sms_messages (device_id, sender, text, timestamp, received_at)
           VALUES (?, ?, ?, ?, ?)""",
        (device_id, sender, text, timestamp, time.time()),
    )
    conn.commit()
    conn.close()
    log.info("Saved single SMS from %s", sender)


def save_part(
    device_id: str,
    sender: str,
    reference: int,
    total_parts: int,
    part_number: int,
    text: str,
):
    """Save one part of a multipart SMS. Returns True if saved (not duplicate)."""
    conn = _connect()
    try:
        conn.execute(
            """INSERT OR IGNORE INTO sms_parts
               (device_id, sender, reference, total_parts, part_number, text, received_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (device_id, sender, reference, total_parts, part_number, text, time.time()),
        )
        conn.commit()
        inserted = conn.total_changes > 0
    except Exception:
        log.exception("Failed to save part")
        inserted = False
    finally:
        conn.close()

    log.info(
        "Part %d/%d (ref=%d) from %s — %s",
        part_number, total_parts, reference, sender,
        "saved" if inserted else "duplicate",
    )
    return inserted


def try_assemble(
    device_id: str, sender: str, reference: int, total_parts: int, timestamp: str
) -> str | None:
    """
    Check if all parts of a multipart SMS are present.
    If yes, assemble, save to sms_messages, delete parts, return full text.
    If no, return None.
    """
    conn = _connect()

    rows = conn.execute(
        """SELECT part_number, text FROM sms_parts
           WHERE device_id = ? AND sender = ? AND reference = ?
           ORDER BY part_number""",
        (device_id, sender, reference),
    ).fetchall()

    if len(rows) < total_parts:
        conn.close()
        log.info(
            "Multipart ref=%d: %d/%d parts received",
            reference, len(rows), total_parts,
        )
        return None

    # Assemble
    full_text = "".join(row["text"] for row in rows)

    # Save assembled message
    conn.execute(
        """INSERT INTO sms_messages (device_id, sender, text, timestamp, received_at)
           VALUES (?, ?, ?, ?, ?)""",
        (device_id, sender, full_text, timestamp, time.time()),
    )

    # Delete parts
    conn.execute(
        """DELETE FROM sms_parts
           WHERE device_id = ? AND sender = ? AND reference = ?""",
        (device_id, sender, reference),
    )

    conn.commit()
    conn.close()

    log.info(
        "Assembled multipart ref=%d (%d parts) from %s: %d chars",
        reference, total_parts, sender, len(full_text),
    )
    return full_text


def cleanup_stale():
    """Delete multipart parts older than MULTIPART_TIMEOUT_SEC."""
    cutoff = time.time() - config.MULTIPART_TIMEOUT_SEC
    conn = _connect()
    cursor = conn.execute(
        "DELETE FROM sms_parts WHERE received_at < ?", (cutoff,)
    )
    deleted = cursor.rowcount
    conn.commit()
    conn.close()

    if deleted > 0:
        log.info("Cleaned up %d stale multipart parts", deleted)
