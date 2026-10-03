"""
Gateway health tracking.

A gateway is healthy when it is online AND its SIM is registered in the cellular network.
Telegram alerts are debounced: a problem is reported only if it lasts longer than a
threshold, recovery only if a problem was reported (or the gateway was rebooted by command)
and the gateway stayed healthy for a hold period. Short blips are kept in a 24h incident log
(shown by /status) but never reach the chat.
"""

import logging
import threading
import time
from collections import deque

import config
import telegram

log = logging.getLogger(__name__)

TICK_SEC = 5
INCIDENT_WINDOW_SEC = 24 * 3600
REBOOT_WAIT_SEC = 300  # сколько ждём, что после /reboot шлюз вообще пропадёт и вернётся

_THRESHOLDS = {
    "offline": lambda: config.ALERT_OFFLINE_AFTER_SEC,
    "network": lambda: config.ALERT_NETWORK_AFTER_SEC,
}

_states = {}
_lock = threading.RLock()


class _State:
    def __init__(self, gateway):
        self.gateway = gateway
        self.online = None          # None — ещё нет данных
        self.registered = None
        self.operator = ""
        self.roaming = False
        self.since = {}             # причина -> когда началась (offline | network)
        self.alerted = set()        # причины, о которых уже сообщили
        self.healthy_since = None
        self.down_since = None      # начало текущего простоя (для длительности в сообщении)
        self.incident_start = None  # начало текущего инцидента (для журнала)
        self.incidents = deque()    # (start, end) за последние сутки
        self.reboot_since = None    # ждём возврата после /reboot
        self.reboot_seen_down = False


def _state(gateway):
    st = _states.get(gateway["name"])
    if st is None:
        st = _states[gateway["name"]] = _State(gateway)
    return st


def _reasons(st):
    if st.online is False:
        return {"offline"}
    if st.registered is False:
        return {"network"}
    return set()


def _apply(st):
    """Recompute problem timers after an input changed (call with _lock held)."""
    now = time.time()
    reasons = _reasons(st)
    for r in reasons:
        st.since.setdefault(r, now)
    for r in list(st.since):
        if r not in reasons:
            del st.since[r]

    if reasons:
        st.healthy_since = None
        if st.incident_start is None:
            st.incident_start = now
        if st.down_since is None:
            st.down_since = now
        if st.reboot_since is not None:
            st.reboot_seen_down = True
    else:
        if st.healthy_since is None:
            st.healthy_since = now
        if st.incident_start is not None:
            st.incidents.append((st.incident_start, now))
            st.incident_start = None


def set_online(gateway, online: bool):
    with _lock:
        st = _state(gateway)
        st.online = online
        _apply(st)


def set_network(gateway, registered: bool, operator: str, roaming: bool):
    with _lock:
        st = _state(gateway)
        if operator and operator != st.operator and st.operator:
            log.info("Gateway %s operator: %s -> %s", gateway["name"], st.operator, operator)
        st.registered = registered
        st.operator = operator or st.operator
        st.roaming = roaming
        _apply(st)


def expect_reboot(gateway):
    """A reboot was requested from Telegram: announce when the gateway is back."""
    with _lock:
        st = _state(gateway)
        st.reboot_since = time.time()
        st.reboot_seen_down = False


def incidents(gateway):
    """Return (count, total_seconds) of incidents in the last 24h, including an open one."""
    now = time.time()
    with _lock:
        st = _states.get(gateway["name"])
        if st is None:
            return 0, 0
        while st.incidents and st.incidents[0][1] < now - INCIDENT_WINDOW_SEC:
            st.incidents.popleft()
        spans = list(st.incidents)
        if st.incident_start is not None:
            spans.append((st.incident_start, now))
    return len(spans), int(sum(end - start for start, end in spans))


def _fmt_duration(sec):
    sec = int(sec)
    if sec < 90:
        return f"{sec} с"
    if sec < 5400:
        return f"{round(sec / 60)} мин"
    return f"{sec // 3600} ч {sec % 3600 // 60} мин"


fmt_duration = _fmt_duration


def _alert_text(st, reason):
    name = telegram.escape_html(st.gateway["name"])
    if reason == "offline":
        return f"⚠️ <b>{name}</b> недоступен (offline)"
    return f"📵 <b>{name}</b>: SIM-карта не зарегистрирована в сети"


def _recovery_text(st, rebooted):
    name = telegram.escape_html(st.gateway["name"])
    op = telegram.escape_html(st.operator) if st.operator else "?"
    sim = f"SIM: {op}" + (" (роуминг)" if st.roaming else "")
    head = "перезагружен и работает" if rebooted and not st.alerted else "снова работает"
    dur = (
        f", простой {_fmt_duration(st.healthy_since - st.down_since)}" if st.down_since else ""
    )
    return f"✅ <b>{name}</b> {head} — {sim}{dur}"


def _tick():
    now = time.time()
    outbox = []
    with _lock:
        for st in _states.values():
            # проблема продержалась дольше порога — сообщаем
            for reason, since in st.since.items():
                if reason not in st.alerted and now - since >= _THRESHOLDS[reason]():
                    st.alerted.add(reason)
                    outbox.append((st.gateway, _alert_text(st, reason)))

            healthy = not st.since and st.healthy_since is not None
            held = healthy and now - st.healthy_since >= config.ALERT_RECOVER_HOLD_SEC

            if held:
                rebooted = st.reboot_since is not None and st.reboot_seen_down
                if st.alerted or rebooted:
                    outbox.append((st.gateway, _recovery_text(st, rebooted)))
                st.alerted.clear()
                st.down_since = None
                st.reboot_since = None
                st.reboot_seen_down = False

            if st.reboot_since is not None and now - st.reboot_since > REBOOT_WAIT_SEC:
                st.reboot_since = None  # шлюз так и не пропал — отмечаем без сообщения
                st.reboot_seen_down = False

    for gateway, text in outbox:  # отправка вне блокировки
        telegram.send_text(text, gateway["telegram_bot_token"], gateway["telegram_chat_id"])


def run_loop(is_running):
    while is_running():
        time.sleep(TICK_SEC)
        try:
            _tick()
        except Exception:
            log.exception("Health tick failed")
