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
int bootLine = 2;  // текущая строка для boot-сообщений (после заголовка + разделителя)

// ── Хелперы ─────────────────────────────────────────

void drawSeparator(int y) {
    oled.drawFastHLine(0, y, OLED_WIDTH, SSD1306_WHITE);
}

void drawInvertedLine(int y, const char* text) {
    int textWidth = strlen(text) * 6;
    int x = (OLED_WIDTH - textWidth) / 2;  // центрирование

    oled.fillRect(0, y, OLED_WIDTH, 8, SSD1306_WHITE);
    oled.setTextColor(SSD1306_BLACK);
    oled.setCursor(x, y);
    oled.print(text);
    oled.setTextColor(SSD1306_WHITE);
}

int rssiToPercent(int rssi) {
    int pct = 2 * (rssi + 100);
    if (pct < 0) pct = 0;
    if (pct > 100) pct = 100;
    return pct;
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

    // Инверсный заголовок с DEVICE_ID (центрирован)
    drawInvertedLine(0, DEVICE_ID);
    drawSeparator(9);

    oled.display();
    Serial.println("[OLED] Init OK");
}

void displayBoot(const char* label, bool ok) {
    int y = bootLine * 8;

    oled.setCursor(0, y);
    oled.print(label);
    oled.print("...");

    // [OK] или [FAIL] по правому краю
    const char* status = ok ? "[OK]" : "[FAIL]";
    int statusX = OLED_WIDTH - strlen(status) * 6;
    oled.setCursor(statusX, y);
    oled.print(status);

    oled.display();
    bootLine++;
}

void displayBootReady() {
    drawSeparator(bootLine * 8);
    bootLine++;

    oled.setCursor(0, bootLine * 8);
    oled.print("Ready!");

    oled.display();
}

// ── Макет статус-экрана ─────────────────────────────
//
//  y= 0 (8px)  ██████ gate-Yauheni ██████   инверсный заголовок
//  y= 9 (1px)  ──────────────────────────    разделитель
//  y=12 (8px)  📶 Eriwireless        66%    WiFi
//  y=20 (8px)  ☁  10.9.7.20           OK    MQTT
//  y=29 (1px)  ──────────────────────────    разделитель
//  y=32 (16px) 📱 SMS: 12/0                 шрифт 2x!
//  y=49 (1px)  ──────────────────────────    разделитель
//  y=52 (8px)  🕐 2d 05:32:10       234KB   uptime + heap
//              итого: 60px

void displayStatus() {
    oled.clearDisplay();
    oled.setTextSize(1);
    oled.setTextColor(SSD1306_WHITE);

    // ── Заголовок (y=0, 8px) ──
    drawInvertedLine(0, DEVICE_ID);

    // ── Разделитель (y=9) ──
    drawSeparator(9);

    // ── WiFi (y=12, 8px, шрифт 1x) ──
    oled.drawBitmap(0, 12, icon_wifi, 8, 8, SSD1306_WHITE);
    {
        int pct = rssiToPercent(WiFi.RSSI());
        char line[22];
        String ssid = WiFi.SSID();
        if (ssid.length() > 14) ssid = ssid.substring(0, 14);
        snprintf(line, sizeof(line), "%-14s %3d%%", ssid.c_str(), pct);
        oled.setCursor(10, 12);
        oled.print(line);
    }

    // ── MQTT (y=20, 8px, шрифт 1x) ──
    oled.drawBitmap(0, 20, icon_mqtt, 8, 8, SSD1306_WHITE);
    {
        char line[22];
        const char* status = mqtt.connected() ? "OK" : "OFF";
        snprintf(line, sizeof(line), "%-15s %s", MQTT_BROKER, status);
        oled.setCursor(10, 20);
        oled.print(line);
    }

    // ── Разделитель (y=29) ──
    drawSeparator(29);

    // ── SMS крупным шрифтом (y=32, 16px, шрифт 2x) ──
    oled.drawBitmap(0, 32, icon_sim16, 16, 16, SSD1306_WHITE);
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
        oled.setCursor(x, 32);
        oled.print(line);
        oled.setTextSize(1);  // вернуть шрифт
    }

    // ── Разделитель (y=49) ──
    drawSeparator(49);

    // ── Uptime + Heap (y=52, 8px, шрифт 1x) ──
    oled.drawBitmap(0, 52, icon_clock, 8, 8, SSD1306_WHITE);
    {
        char uptimeBuf[16];
        uint32_t sec = (millis() - startTime) / 1000;
        formatUptime(sec, uptimeBuf);
        oled.setCursor(10, 52);
        oled.print(uptimeBuf);

        // Heap по правому краю в KB
        uint32_t heapKB = ESP.getFreeHeap() / 1024;
        char heapBuf[8];
        snprintf(heapBuf, sizeof(heapBuf), "%luKB", heapKB);
        int heapX = OLED_WIDTH - strlen(heapBuf) * 6;
        oled.setCursor(heapX, 52);
        oled.print(heapBuf);
    }

    oled.display();
}
