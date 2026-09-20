#pragma once

#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include "config.h"

// ── Extern-переменные из main.cpp ───────────────────
extern bool     sim900Ok;
extern uint32_t smsForwarded;
extern uint32_t smsPending;
extern uint32_t startTime;
extern PubSubClient mqtt;

// ── Bitmap-иконки 8x8 ──────────────────────────────

// WiFi — дуги сигнала
static const uint8_t PROGMEM icon_wifi[] = {
    0b00011000,
    0b01100110,
    0b10000001,
    0b00011000,
    0b01100110,
    0b00000000,
    0b00011000,
    0b00011000
};

// MQTT — облако
static const uint8_t PROGMEM icon_mqtt[] = {
    0b00111000,
    0b01000100,
    0b10000010,
    0b10000011,
    0b10000001,
    0b11111111,
    0b01111110,
    0b00000000
};

// SIM — карточка (16x16 для крупной SMS-строки)
static const uint8_t PROGMEM icon_sim16[] = {
    0b00111111, 0b11000000,
    0b00100000, 0b01100000,
    0b00100000, 0b00100000,
    0b00100000, 0b00100000,
    0b00111111, 0b11100000,
    0b00100000, 0b00100000,
    0b00101010, 0b10100000,
    0b00100101, 0b00100000,
    0b00101010, 0b10100000,
    0b00100101, 0b00100000,
    0b00101010, 0b10100000,
    0b00100101, 0b00100000,
    0b00101010, 0b10100000,
    0b00111111, 0b11100000,
    0b00000000, 0b00000000,
    0b00000000, 0b00000000
};

// Clock — часы
static const uint8_t PROGMEM icon_clock[] = {
    0b00111100,
    0b01000010,
    0b10010001,
    0b10010001,
    0b10011101,
    0b10000001,
    0b01000010,
    0b00111100
};

// ── Объект дисплея ──────────────────────────────────

Adafruit_SSD1306 oled(OLED_WIDTH, OLED_HEIGHT, &Wire, -1);

// ── Геометрия двухцветной панели ────────────────────
// Верхние 16px — ЖЁЛТАЯ зона, ниже (y>=16) — СИНЯЯ. На стыке (~y=16) у панели
// физический зазор, поэтому ничего не рисуем поперёк него: заголовок держим
// целиком в жёлтой зоне, весь контент — в синей (с отступом от стыка).
static const int HEADER_H = 16;   // высота жёлтой зоны
static const int BODY_TOP = 18;   // первая строка контента в синей зоне

int bootY = BODY_TOP;  // текущая Y для boot-сообщений (в синей зоне)

// ── Хелперы ─────────────────────────────────────────

void drawSeparator(int y) {
    oled.drawFastHLine(0, y, OLED_WIDTH, SSD1306_WHITE);
}

int rssiToPercent(int rssi) {
    int pct = 2 * (rssi + 100);
    if (pct < 0) pct = 0;
    if (pct > 100) pct = 100;
    return pct;
}

void drawHeader(const char* text) {
    // Жёлтый заголовок во всю жёлтую зону (0..15):
    //   слева  — имя шлюза, справа — иконка WiFi + сигнал в %.
    // Всё чёрным по сплошной жёлтой заливке; нижняя кромка на стыке (y=16).
    int y = (HEADER_H - 8) / 2;   // вертикальный центр 8px строки → y=4

    oled.fillRect(0, 0, OLED_WIDTH, HEADER_H, SSD1306_WHITE);
    oled.setTextColor(SSD1306_BLACK);

    // Слева: имя шлюза
    oled.setCursor(2, y);
    oled.print(text);

    // Справа: сигнал в % по правому краю (или "--", если WiFi не подключён)
    char pct[6];
    if (WiFi.status() == WL_CONNECTED) {
        snprintf(pct, sizeof(pct), "%d%%", rssiToPercent(WiFi.RSSI()));
    } else {
        snprintf(pct, sizeof(pct), "--");
    }
    int pctX = OLED_WIDTH - 2 - (int)strlen(pct) * 6;
    oled.setCursor(pctX, y);
    oled.print(pct);

    // Иконка WiFi слева от процентов (чёрным по жёлтому)
    oled.drawBitmap(pctX - 10, y, icon_wifi, 8, 8, SSD1306_BLACK);

    oled.setTextColor(SSD1306_WHITE);
}

void formatUptime(uint32_t sec, char* buf) {
    uint32_t days = sec / 86400;
    uint32_t hours = (sec % 86400) / 3600;
    uint32_t mins = (sec % 3600) / 60;
    uint32_t secs = sec % 60;

    if (days > 0) {
        sprintf(buf, "%lud %02lu:%02lu:%02lu", days, hours, mins, secs);
    } else {
        sprintf(buf, "%02lu:%02lu:%02lu", hours, mins, secs);
    }
}

// ── Функции дисплея ─────────────────────────────────

void displayInit() {
    Wire.begin(OLED_SDA, OLED_SCL);

    if (!oled.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR)) {
        Serial.println("[OLED] Init FAILED");
        return;
    }

    oled.clearDisplay();
    oled.setTextSize(1);
    oled.setTextColor(SSD1306_WHITE);

    // Заголовок с DEVICE_ID на всю жёлтую зону
    drawHeader(DEVICE_ID);

    oled.display();
    Serial.println("[OLED] Init OK");
}

void displayBoot(const char* label, bool ok) {
    oled.setCursor(0, bootY);
    oled.print(label);
    oled.print("...");

    // [OK] или [FAIL] по правому краю
    const char* status = ok ? "[OK]" : "[FAIL]";
    int statusX = OLED_WIDTH - strlen(status) * 6;
    oled.setCursor(statusX, bootY);
    oled.print(status);

    oled.display();
    bootY += 9;
}

void displayBootReady() {
    drawSeparator(bootY);

    oled.setCursor(0, bootY + 3);
    oled.print("Ready!");

    oled.display();
}

// ── Макет статус-экрана ─────────────────────────────
// Заголовок (жёлтая зона) несёт имя шлюза + сигнал WiFi; синяя зона — остальное.
// Стык (y=16) не пересекает ни один элемент.
//
//  y= 0..15 (16px) gate-Alena        📶 66%   ЖЁЛТЫЙ заголовок (== жёлтая зона)
//  ── стык цветов на y=16 ──
//  y=20 (8px)  ☁  10.9.7.20           OK    MQTT   (синяя зона)
//  y=31 (1px)  ──────────────────────────    разделитель
//  y=35 (16px) 📱 SMS: 12/0                 шрифт 2x
//  y=53 (1px)  ──────────────────────────    разделитель
//  y=55 (8px)  🕐 2d 05:32:10       234KB   uptime + heap → до y=62

void displayStatus() {
    oled.clearDisplay();
    oled.setTextSize(1);
    oled.setTextColor(SSD1306_WHITE);

    // ── Заголовок: имя шлюза + WiFi (вся жёлтая зона 0..15) ──
    drawHeader(DEVICE_ID);

    // ── MQTT (y=20, синяя зона, шрифт 1x) ──
    oled.drawBitmap(0, 20, icon_mqtt, 8, 8, SSD1306_WHITE);
    {
        char line[22];
        const char* status = mqtt.connected() ? "OK" : "OFF";
        snprintf(line, sizeof(line), "%-15s %s", MQTT_BROKER, status);
        oled.setCursor(10, 20);
        oled.print(line);
    }

    // ── Разделитель (y=31) ──
    drawSeparator(31);

    // ── SMS крупным шрифтом (y=35, 16px, шрифт 2x) ──
    oled.drawBitmap(0, 35, icon_sim16, 16, 16, SSD1306_WHITE);
    {
        oled.setTextSize(2);
        char line[11];
        if (smsForwarded >= 1000) {
            // Компактный формат для больших чисел
            snprintf(line, sizeof(line), "%luK/%lu", smsForwarded / 1000, smsPending);
        } else {
            snprintf(line, sizeof(line), "SMS:%lu/%lu", smsForwarded, smsPending);
        }
        // Центрируем текст в оставшемся пространстве (от x=18 до x=128)
        int textWidth = strlen(line) * 12;  // шрифт 2x = 12px на символ
        int x = 18 + (110 - textWidth) / 2;
        if (x < 18) x = 18;
        oled.setCursor(x, 35);
        oled.print(line);
        oled.setTextSize(1);  // вернуть шрифт
    }

    // ── Разделитель (y=53) ──
    drawSeparator(53);

    // ── Uptime + Heap (y=55, 8px, шрифт 1x) ──
    oled.drawBitmap(0, 55, icon_clock, 8, 8, SSD1306_WHITE);
    {
        char uptimeBuf[16];
        uint32_t sec = (millis() - startTime) / 1000;
        formatUptime(sec, uptimeBuf);
        oled.setCursor(10, 55);
        oled.print(uptimeBuf);

        // Heap по правому краю в KB
        uint32_t heapKB = ESP.getFreeHeap() / 1024;
        char heapBuf[8];
        snprintf(heapBuf, sizeof(heapBuf), "%luKB", heapKB);
        int heapX = OLED_WIDTH - strlen(heapBuf) * 6;
        oled.setCursor(heapX, 55);
        oled.print(heapBuf);
    }

    oled.display();
}
