"""
PDU (Protocol Data Unit) parser for SMS-DELIVER messages.
Handles SMSC, sender address, UDH (multipart), GSM 7-bit and UCS-2 encoding.
"""

# GSM 7-bit default alphabet (basic table)
GSM7_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅå"
    "Δ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ"
    " !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmnopqrstuvwxyzäöñüà"
)


def _hex_to_bytes(hex_str: str) -> bytes:
    return bytes.fromhex(hex_str)


def _decode_semi_octet(data: bytes) -> str:
    """Decode phone number in semi-octet (swapped nibble) format."""
    result = ""
    for b in data:
        lo = b & 0x0F
        hi = (b >> 4) & 0x0F
        result += str(lo)
        if hi != 0x0F:
            result += str(hi)
    return result


def _decode_timestamp(data: bytes) -> str:
    """Decode TP-SCTS (7 bytes, semi-octet) to readable timestamp."""
    parts = []
    for b in data:
        lo = b & 0x0F
        hi = (b >> 4) & 0x0F
        parts.append(lo * 10 + hi)

    if len(parts) < 7:
        return "unknown"

    year = 2000 + parts[0]
    tz_sign = "-" if (data[6] & 0x08) else "+"
    tz_no_sign = data[6] & ~0x08  # clear sign bit
    tz_quarters = (tz_no_sign & 0x0F) * 10 + ((tz_no_sign >> 4) & 0x0F)
    tz_hours = tz_quarters // 4
    tz_mins = (tz_quarters % 4) * 15

    return (
        f"{parts[2]:02d}.{parts[1]:02d}.{year:04d} "
        f"{parts[3]:02d}:{parts[4]:02d}:{parts[5]:02d} "
        f"{tz_sign}{tz_hours:02d}:{tz_mins:02d}"
    )


def _decode_gsm7(data: bytes, num_chars: int, offset_bits: int = 0) -> str:
    """Decode GSM 7-bit packed data."""
    result = []
    bit_pos = offset_bits

    for _ in range(num_chars):
        byte_idx = bit_pos // 8
        bit_offset = bit_pos % 8

        if byte_idx >= len(data):
            break

        char_code = (data[byte_idx] >> bit_offset) & 0x7F
        if bit_offset > 1 and byte_idx + 1 < len(data):
            char_code |= (data[byte_idx + 1] << (8 - bit_offset)) & 0x7F

        if char_code < len(GSM7_BASIC):
            result.append(GSM7_BASIC[char_code])
        else:
            result.append("?")

        bit_pos += 7

    return "".join(result)


def _decode_ucs2(data: bytes) -> str:
    """Decode UCS-2 (UTF-16 BE) data."""
    return data.decode("utf-16-be", errors="replace")


def _parse_udh(data: bytes):
    """Parse User Data Header. Returns (udh_length_bytes, multipart_info or None)."""
    if len(data) < 1:
        return 0, None

    udh_len = data[0]
    if len(data) < 1 + udh_len:
        return 0, None

    multipart = None
    pos = 1
    while pos < 1 + udh_len:
        if pos + 1 >= len(data):
            break

        iei = data[pos]
        iel = data[pos + 1]

        if iei == 0x00 and iel == 3 and pos + 4 < len(data):
            # Concatenated SMS, 8-bit reference
            multipart = {
                "reference": data[pos + 2],
                "total": data[pos + 3],
                "part": data[pos + 4],
            }
        elif iei == 0x08 and iel == 4 and pos + 5 < len(data):
            # Concatenated SMS, 16-bit reference
            multipart = {
                "reference": (data[pos + 2] << 8) | data[pos + 3],
                "total": data[pos + 4],
                "part": data[pos + 5],
            }

        pos += 2 + iel

    return 1 + udh_len, multipart


def parse(pdu_hex: str) -> dict:
    """
    Parse SMS-DELIVER PDU.

    Returns:
        {
            "sender": "+...",
            "text": "...",
            "timestamp": "...",
            "multipart": {"reference": int, "total": int, "part": int} | None
        }
    """
    data = _hex_to_bytes(pdu_hex)
    pos = 0

    # ── SMSC (Service Center Address) ───────────────────
    smsc_len = data[pos]
    pos += 1 + smsc_len

    # ── PDU type (first octet of SMS-DELIVER) ──────────
    pdu_type = data[pos]
    pos += 1
    has_udhi = bool(pdu_type & 0x40)  # User Data Header Indicator

    # ── Originating Address (sender) ───────────────────
    oa_len = data[pos]  # number of digits
    pos += 1
    oa_type = data[pos]
    pos += 1

    oa_bytes_count = (oa_len + 1) // 2
    oa_number = _decode_semi_octet(data[pos : pos + oa_bytes_count])
    pos += oa_bytes_count

    # Format phone number
    if (oa_type & 0x70) == 0x10:  # international
        sender = "+" + oa_number
    else:
        sender = oa_number

    # ── TP-PID ─────────────────────────────────────────
    pos += 1  # Protocol Identifier

    # ── TP-DCS (Data Coding Scheme) ────────────────────
    dcs = data[pos]
    pos += 1

    # Determine encoding
    if (dcs & 0x0C) == 0x08:
        encoding = "ucs2"
    elif (dcs & 0x0C) == 0x00:
        encoding = "gsm7"
    else:
        encoding = "8bit"

    # ── TP-SCTS (Timestamp, 7 bytes) ──────────────────
    timestamp = _decode_timestamp(data[pos : pos + 7])
    pos += 7

    # ── TP-UDL (User Data Length) ─────────────────────
    udl = data[pos]
    pos += 1

    # ── TP-UD (User Data) ────────────────────────────
    ud = data[pos:]

    multipart = None
    text = ""

    if has_udhi:
        udh_bytes, multipart = _parse_udh(ud)

        if encoding == "gsm7":
            # UDH occupies ceil((udh_bytes * 8) / 7) septets
            udh_septets = ((udh_bytes * 8) + 6) // 7
            fill_bits = (udh_septets * 7) - (udh_bytes * 8)
            text_septets = udl - udh_septets
            text = _decode_gsm7(ud[udh_bytes:], text_septets, fill_bits)
        elif encoding == "ucs2":
            text = _decode_ucs2(ud[udh_bytes:])
        else:
            text = ud[udh_bytes:].hex()
    else:
        if encoding == "gsm7":
            text = _decode_gsm7(ud, udl)
        elif encoding == "ucs2":
            text = _decode_ucs2(ud)
        else:
            text = ud.hex()

    return {
        "sender": sender,
        "text": text,
        "timestamp": timestamp,
        "multipart": multipart,
    }
