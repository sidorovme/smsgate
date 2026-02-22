# SMS Gateway

SMS-шлюз на базе ESP32 + SIM900: принимает входящие SMS, передаёт через MQTT на бэкенд, который парсит PDU, склеивает multipart-сообщения и отправляет уведомления в Telegram.

## Архитектура

```
┌─────────────┐     UART      ┌─────────────┐     MQTT        ┌─────────────┐     HTTPS      ┌──────────┐
│   SIM900    │ ─────────────▶ │    ESP32    │ ──────────────▶ │   Backend   │ ──────────────▶ │ Telegram │
│  GSM-модем  │   AT-команды   │  Прошивка   │  sms/incoming   │   Python    │   Bot API      │          │
└─────────────┘                └─────────────┘                 └──────┬──────┘                 └──────────┘
                                                                      │
                                                                 ┌────▼────┐
                                                                 │ SQLite  │
                                                                 └─────────┘
```

## Структура проекта

```
smsgate/
├── embedded/              # Прошивка ESP32 (PlatformIO)
│   ├── platformio.ini
│   └── src/
│       ├── config.h       # Настройки WiFi, MQTT, SIM900
│       └── main.cpp       # Основная логика
│
├── backend/               # Бэкенд на Python (Raspberry Pi)
│   ├── main.py            # Точка входа, MQTT-клиент
│   ├── pdu_parser.py      # Парсинг SMS PDU (SMSC, отправитель, UDH, текст)
│   ├── db.py              # SQLite: хранение частей, склейка multipart
│   ├── telegram.py        # Отправка уведомлений в Telegram
│   ├── config.py          # Настройки (MQTT, Telegram, БД)
│   ├── requirements.txt   # Python-зависимости
│   └── smsgate-backend.service  # systemd unit-файл
│
└── README.md
```

## Embedded (ESP32 + SIM900)

### Железо

- **ESP32-WROOM-32** — микроконтроллер с WiFi
- **SIM900** — GSM-модем для приёма SMS
- Подключение: UART (GPIO16 RX, GPIO17 TX)
- Управление питанием: GPIO4 (D4) — HIGH включает модем
- Питание: общий 5В блок (минимум 2.5А), ESP32 через Vin

### Что делает

1. Включает питание SIM900 через GPIO4 (D4 HIGH)
2. Подключается к WiFi и MQTT-брокеру
3. Слушает SIM900 по UART в PDU-режиме
4. При получении SMS (`+CMTI`) — читает PDU, публикует в MQTT-топик `sms/incoming`
5. Удаляет SMS с SIM-карты после успешной отправки
6. Каждые 30 сек опрашивает SIM на неотправленные SMS (ретрай)
7. Автоопределение baud rate SIM900 (115200 → 9600)
8. HTTP-эндпоинты для управления и мониторинга (см. ниже)

### HTTP-эндпоинты

| Эндпоинт | Описание |
|---|---|
| `GET /status` | JSON с метриками: uptime, RSSI, IP, кол-во SMS, heap, состояние модема |
| `GET /reboot` | Перезагрузка ESP32 |
| `GET /reset-modem` | Power cycle SIM900: выкл → вкл питание, переинициализация |

### MQTT-сообщение

```json
{
  "pdu": "07919995599999F9440C91...",
  "device_id": "gate-01",
  "sim_index": 1
}
```

### Сборка и прошивка

```bash
cd embedded
pio run -t upload
pio device monitor          # просмотр логов
```

## Backend (Python)

### Что делает

1. Подписывается на MQTT-топик `sms/incoming`
2. Парсит PDU — извлекает отправителя, текст, timestamp, UDH (multipart-информация)
3. Поддерживаемые кодировки: **UCS-2** (Unicode), **GSM 7-bit**
4. Одиночные SMS — сразу сохраняет в БД и отправляет в Telegram
5. Multipart SMS — сохраняет части, при получении всех — склеивает и отправляет
6. Автоочистка незавершённых multipart через 5 минут

### Настройка

Отредактировать `backend/config.py`:

```python
MQTT_BROKER = "1.2.3.4"
MQTT_PORT = 1883
MQTT_TOPIC = "sms/incoming"
TELEGRAM_BOT_TOKEN = "your-bot-token"
TELEGRAM_CHAT_ID = "your-chat-id"
```

### Установка и запуск

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py
```

### Запуск как systemd-сервис

```bash
sudo cp smsgate-backend.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable smsgate-backend
sudo systemctl start smsgate-backend

# Логи
journalctl -u smsgate-backend -f
```

## Формат Telegram-уведомления

```
📩 New SMS

SIM: gate-01
Sender: +1234567890123
Time: 2026-02-11 23:42:50 +04:00

Текст сообщения (склеенный из всех частей)
```

## Схема подключения

```
ESP32 GPIO16 (RX) ◄──── SIM900 TX (3.3V TTL)
ESP32 GPIO17 (TX) ────► SIM900 RX
ESP32 GPIO4  (D4) ────► Управление питанием SIM900 (HIGH = вкл)
ESP32 GND ─────────────  SIM900 GND ─────── БП GND
ESP32 Vin ◄────────────────────────────────── БП 5V
SIM900 Vin ◄───────────────────────────────── БП 5V
```

> Если SIM900 имеет только 5В линии TX/RX — на линии SIM900 TX → ESP32 RX нужен делитель напряжения (1кОм + 2кОм).
