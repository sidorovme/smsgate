"""
PDU encoder for SMS-SUBMIT messages.
GSM 7-bit (with extension table) or UCS-2, automatic multipart split via UDH.
Every part requests a delivery report (TP-SRR).
"""

import re

from pdu_parser import GSM7_BASIC

# Расширенная таблица GSM 03.38 (символ → код после ESC 0x1B)
GSM7_EXT = {
    "\f": 0x0A, "^": 0x14, "{": 0x28, "}": 0x29, "\\": 0x2F,
    "[": 0x3C, "~": 0x3D, "]": 0x3E, "|": 0x40, "€": 0x65,
}

_BASIC_MAP = {ch: i for i, ch in enumerate(GSM7_BASIC) if i != 0x1B}

_NUMBER_RE = re.compile(r"^\+?\d{5,15}$")


class EncodeError(ValueError):
    pass


def normalize_number(raw: str) -> str:
    """Strip separators; raise EncodeError if not a plausible phone number."""
    number = re.sub(r"[\s\-().]", "", raw)
    if not _NUMBER_RE.match(number):
        raise EncodeError("некорректный номер")
    return number


def _to_gsm7(text: str):
    """Return list of septets (ESC pairs kept adjacent) or None if not representable."""
    septets = []
    for ch in text:
        if ch in _BASIC_MAP:
            septets.append(_BASIC_MAP[ch])
        elif ch in GSM7_EXT:
            septets.extend((0x1B, GSM7_EXT[ch]))
        else:
            return None
    return septets


def _pack7(septets, fill_bits=0) -> bytes:
    out = bytearray()
    acc = 0
    nbits = fill_bits
    for s in septets:
        acc |= s << nbits
        nbits += 7
        while nbits >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            nbits -= 8
    if nbits > 0:
        out.append(acc & 0xFF)
    return bytes(out)


def _swap_digits(digits: str) -> bytes:
    if len(digits) % 2:
        digits += "F"
    return bytes((int(digits[i + 1], 16) << 4) | int(digits[i], 16) for i in range(0, len(digits), 2))


def _split_gsm7(septets, size):
    parts, cur = [], []
    i = 0
    while i < len(septets):
        step = 2 if septets[i] == 0x1B else 1
        if len(cur) + step > size:
            parts.append(cur)
            cur = []
        cur.extend(septets[i:i + step])
        i += step
    if cur:
        parts.append(cur)
    return parts


def _split_ucs2(text, size):
    """Split into chunks of <= size UTF-16 code units, never inside a surrogate pair."""
    parts, cur, cur_len = [], "", 0
    for ch in text:
        units = 2 if ord(ch) > 0xFFFF else 1
        if cur_len + units > size:
            parts.append(cur)
            cur, cur_len = "", 0
        cur += ch
        cur_len += units
    if cur:
        parts.append(cur)
    return parts


def encode(to: str, text: str, reference: int):
    """
    Encode text for recipient `to` into SMS-SUBMIT PDUs.

    Returns list of (pdu_hex, tpdu_length) — tpdu_length is the value for AT+CMGS=<len>
    (PDU length in octets without the SMSC part).
    """
    number = normalize_number(to)
    if not text:
        raise EncodeError("пустой текст")

    international = number.startswith("+")
    digits = number.lstrip("+")
    da = bytes([len(digits), 0x91 if international else 0x81]) + _swap_digits(digits)

    septets = _to_gsm7(text)
    if septets is not None:
        dcs = 0x00
        single = len(septets) <= 160
        chunks = [septets] if single else _split_gsm7(septets, 153)
    else:
        dcs = 0x08
        units = len(text.encode("utf-16-be")) // 2
        single = units <= 70
        chunks = [text] if single else _split_ucs2(text, 67)

    total = len(chunks)
    result = []
    for idx, chunk in enumerate(chunks, start=1):
        first = 0x01 | 0x20  # SMS-SUBMIT + status report request
        udh = b""
        if not single:
            first |= 0x40
            udh = bytes([0x05, 0x00, 0x03, reference & 0xFF, total, idx])

        if dcs == 0x00:
            if udh:
                ud = udh + _pack7(chunk, fill_bits=1)  # 6 байт UDH = 48 бит, +1 бит выравнивания
                udl = 7 + len(chunk)
            else:
                ud = _pack7(chunk)
                udl = len(chunk)
        else:
            body = chunk.encode("utf-16-be")
            ud = udh + body
            udl = len(ud)

        tpdu = bytes([first, 0x00]) + da + bytes([0x00, dcs, udl]) + ud
        result.append(("00" + tpdu.hex().upper(), len(tpdu)))
    return result
