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

        CREATE TABLE IF NOT EXISTS sent_messages (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            gateway     TEXT    NOT NULL,
            recipient   TEXT    NOT NULL,
            text        TEXT    NOT NULL,
            parts_total INTEGER NOT NULL,
            state       TEXT    NOT NULL DEFAULT 'pending',  -- pending|sent|failed|delivered|undelivered
            created_at  REAL    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sent_parts (
            msg_id   INTEGER NOT NULL REFERENCES sent_messages(id) ON DELETE CASCADE,
            part_no  INTEGER NOT NULL,
            mr       INTEGER,                                 -- message reference от модема (+CMGS)
            state    TEXT    NOT NULL DEFAULT 'pending',      -- pending|sent|delivered|failed
            PRIMARY KEY (msg_id, part_no)
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


# ── Outgoing SMS ─────────────────────────────────────

def create_sent(gateway: str, recipient: str, text: str, parts_total: int) -> int:
    """Register an outgoing message (state=pending). Returns its id."""
    conn = _connect()
    cur = conn.execute(
        """INSERT INTO sent_messages (gateway, recipient, text, parts_total, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (gateway, recipient, text, parts_total, time.time()),
    )
    msg_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO sent_parts (msg_id, part_no) VALUES (?, ?)",
        [(msg_id, n) for n in range(1, parts_total + 1)],
    )
    conn.commit()
    conn.close()
    return msg_id


def is_pending(msg_id: int) -> bool:
    conn = _connect()
    row = conn.execute("SELECT state FROM sent_messages WHERE id = ?", (msg_id,)).fetchone()
    conn.close()
    return row is not None and row["state"] == "pending"


def fail_pending(msg_id: int):
    """Mark a still-pending message as failed (no response from the gateway)."""
    conn = _connect()
    conn.execute(
        "UPDATE sent_messages SET state = 'failed' WHERE id = ? AND state = 'pending'",
        (msg_id,),
    )
    conn.execute(
        "UPDATE sent_parts SET state = 'failed' WHERE msg_id = ? AND state = 'pending'",
        (msg_id,),
    )
    conn.commit()
    conn.close()


def apply_send_result(msg_id: int, ok: bool, refs: list):
    """
    Apply the gateway's send result. refs — message references (+CMGS) of the parts
    the modem accepted, in order. Returns the message row, or None if unknown/already handled.
    """
    conn = _connect()
    msg = conn.execute(
        "SELECT * FROM sent_messages WHERE id = ? AND state = 'pending'", (msg_id,)
    ).fetchone()
    if msg is None:
        conn.close()
        return None

    for n, mr in enumerate(refs, start=1):
        conn.execute(
            "UPDATE sent_parts SET mr = ?, state = 'sent' WHERE msg_id = ? AND part_no = ?",
            (mr, msg_id, n),
        )
    conn.execute(
        "UPDATE sent_parts SET state = 'failed' WHERE msg_id = ? AND state = 'pending'",
        (msg_id,),
    )
    conn.execute(
        "UPDATE sent_messages SET state = ? WHERE id = ?",
        ("sent" if ok else "failed", msg_id),
    )
    conn.commit()
    conn.close()
    return msg


def apply_delivery_report(gateway: str, mr: int, recipient: str, state: str):
    """
    Match a delivery report to a sent part and update it.
    state: delivered | failed (temporary 'pending' reports must be filtered by the caller).
    Returns (message_row, final_state) when the whole message just reached a final
    state ('delivered' or 'undelivered'), otherwise None.
    """
    tail = "".join(c for c in recipient if c.isdigit())[-9:]
    conn = _connect()
    cutoff = time.time() - 3 * 86400
    rows = conn.execute(
        """SELECT m.*, p.part_no FROM sent_parts p
           JOIN sent_messages m ON m.id = p.msg_id
           WHERE m.gateway = ? AND m.state = 'sent' AND p.mr = ? AND p.state = 'sent'
             AND m.created_at > ?
           ORDER BY m.created_at DESC""",
        (gateway, mr, cutoff),
    ).fetchall()

    target = next(
        (r for r in rows
         if "".join(c for c in r["recipient"] if c.isdigit())[-9:] == tail),
        None,
    )
    if target is None:
        conn.close()
        return None

    conn.execute(
        "UPDATE sent_parts SET state = ? WHERE msg_id = ? AND part_no = ?",
        (state, target["id"], target["part_no"]),
    )
    states = [
        r["state"]
        for r in conn.execute("SELECT state FROM sent_parts WHERE msg_id = ?", (target["id"],))
    ]
    final = None
    if all(s in ("delivered", "failed") for s in states):
        final = "delivered" if all(s == "delivered" for s in states) else "undelivered"
        conn.execute("UPDATE sent_messages SET state = ? WHERE id = ?", (final, target["id"]))
    conn.commit()
    conn.close()
    return (target, final) if final else None
