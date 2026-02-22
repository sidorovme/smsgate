#include <Arduino.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <WebServer.h>
#include <ArduinoJson.h>
#include <esp_task_wdt.h>
#include "config.h"

// ── Глобальные переменные ──────────────────────────────
HardwareSerial simSerial(2);
WebServer server(80);
WiFiClient wifiClient;
PubSubClient mqtt(wifiClient);

uint32_t smsForwarded  = 0;
uint32_t smsPending    = 0;
String   lastError     = "";
bool     sim900Ok      = false;
uint32_t startTime     = 0;
uint32_t lastPollMs    = 0;
String   simBuffer     = "";

// ── Утилиты AT-команд ──────────────────────────────────

// Отправить AT-команду и получить ответ.
// Возвращает полный ответ модуля (до таймаута или до "OK"/"ERROR").
String sendAT(const String& cmd, uint32_t timeout = AT_TIMEOUT) {
    simSerial.println(cmd);
    String response = "";
    uint32_t start = millis();
    while (millis() - start < timeout) {
        while (simSerial.available()) {
            char c = simSerial.read();
            response += c;
        }
        if (response.indexOf("OK") != -1 || response.indexOf("ERROR") != -1) {
            break;
        }
        delay(10);
    }
    Serial.printf("[AT] %s → %s\n", cmd.c_str(), response.c_str());
    return response;
}

// Отправить AT и проверить что ответ содержит "OK".
bool sendATok(const String& cmd, uint32_t timeout = AT_TIMEOUT) {
    return sendAT(cmd, timeout).indexOf("OK") != -1;
}

// ── Wi-Fi ──────────────────────────────────────────────

void ensureWiFi() {
    if (WiFi.status() == WL_CONNECTED) return;

    Serial.println("[WiFi] Connecting...");
    WiFi.begin(WIFI_SSID, WIFI_PASS);

    uint32_t start = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start < WIFI_RETRY_MS) {
        delay(250);
    }

    if (WiFi.status() == WL_CONNECTED) {
        Serial.printf("[WiFi] Connected: %s\n", WiFi.localIP().toString().c_str());
    } else {
        Serial.println("[WiFi] Not connected, will retry");
    }
}

// ── SIM900 инициализация ───────────────────────────────

bool initSIM900() {
    Serial.println("[SIM] Initializing...");

    // Автоопределение скорости
    const long bauds[] = {115200, 57600, 38400, 19200, 9600};
    const int numBauds = sizeof(bauds) / sizeof(bauds[0]);

    for (int b = 0; b < numBauds && !sim900Ok; b++) {
        Serial.printf("[SIM] Trying %ld baud...\n", bauds[b]);
        simSerial.updateBaudRate(bauds[b]);
        delay(100);

        // Очистить буфер
        while (simSerial.available()) simSerial.read();

        for (int i = 0; i < 3; i++) {
            if (sendATok("AT")) {
                Serial.printf("[SIM] Found at %ld baud\n", bauds[b]);
                sim900Ok = true;
                break;
            }
            delay(500);
        }
    }

    if (!sim900Ok) {
        Serial.println("[SIM] No response from SIM900 at any baud rate");
        lastError = "SIM900 no response";
        return false;
    }

    // Зафиксировать скорость на модуле, чтобы не было autobaud-мусора
    String iprCmd = "AT+IPR=" + String(SIM_BAUD);
    sendATok(iprCmd);
    simSerial.updateBaudRate(SIM_BAUD);
    delay(100);
    while (simSerial.available()) simSerial.read(); // очистить буфер

    sendATok("ATE0");              // отключить эхо
    sendATok("AT+CMGF=0");        // PDU режим
    sendATok("AT+CNMI=2,1,0,0,0"); // уведомление +CMTI при новой SMS

    Serial.println("[SIM] Ready");
    return true;
}

// ── MQTT подключение ─────────────────────────────────

void ensureMQTT() {
    if (mqtt.connected()) return;
    if (WiFi.status() != WL_CONNECTED) return;

    Serial.printf("[MQTT] Connecting to %s:%d...\n", MQTT_BROKER, MQTT_PORT);
    if (mqtt.connect(DEVICE_ID)) {
        Serial.println("[MQTT] Connected");
    } else {
        Serial.printf("[MQTT] Failed, rc=%d\n", mqtt.state());
    }
}

// ── Отправка PDU в MQTT ─────────────────────────────────

bool forwardPDU(const String& pdu, int simIndex) {
    if (WiFi.status() != WL_CONNECTED) {
        lastError = "WiFi disconnected";
        return false;
    }

    ensureMQTT();
    if (!mqtt.connected()) {
        lastError = "MQTT disconnected";
        return false;
    }

    JsonDocument doc;
    doc["pdu"]       = pdu;
    doc["device_id"] = DEVICE_ID;
    doc["sim_index"] = simIndex;

    String body;
    serializeJson(doc, body);

    Serial.printf("[MQTT] Publish %s (%d bytes)\n", MQTT_TOPIC, body.length());

    if (mqtt.publish(MQTT_TOPIC, body.c_str())) {
        Serial.printf("[MQTT] OK (index %d)\n", simIndex);
        smsForwarded++;
        return true;
    }

    lastError = "MQTT publish failed";
    Serial.printf("[MQTT] FAIL: %s\n", lastError.c_str());
    return false;
}

// ── Чтение и пересылка одной SMS по индексу ────────────

// Читает SMS с SIM по индексу, пересылает на endpoint.
// При успехе удаляет SMS с SIM. Возвращает true если доставлена.
bool readAndForward(int index) {
    String resp = sendAT("AT+CMGR=" + String(index), 5000);

    // Ответ формата:
    // +CMGR: <stat>,<alpha>,<length>\r\n
    // <pdu>\r\n
    // OK
    int cmgrPos = resp.indexOf("+CMGR:");
    if (cmgrPos == -1) {
        return false; // пустой слот
    }

    // Найти PDU строку — первая непустая строка после +CMGR:
    int lineEnd = resp.indexOf('\n', cmgrPos);
    if (lineEnd == -1) return false;

    String afterHeader = resp.substring(lineEnd + 1);
    afterHeader.trim();

    // PDU — первая строка (до \r или \n)
    int pduEnd = afterHeader.indexOf('\r');
    if (pduEnd == -1) pduEnd = afterHeader.indexOf('\n');
    if (pduEnd == -1) pduEnd = afterHeader.indexOf('O'); // перед "OK"

    String pdu;
    if (pduEnd > 0) {
        pdu = afterHeader.substring(0, pduEnd);
    } else {
        pdu = afterHeader;
    }
    pdu.trim();

    if (pdu.length() < 10) return false; // слишком короткая, не PDU

    // Убедиться что это hex-строка
    bool validHex = true;
    for (unsigned int i = 0; i < pdu.length(); i++) {
        char c = toupper(pdu[i]);
        if (!((c >= '0' && c <= '9') || (c >= 'A' && c <= 'F'))) {
            validHex = false;
            break;
        }
    }
    if (!validHex) return false;

    Serial.printf("[SMS] Index %d, PDU len=%d\n", index, pdu.length());

    if (forwardPDU(pdu, index)) {
        sendATok("AT+CMGD=" + String(index)); // удалить с SIM
        return true;
    }

    smsPending++;
    return false;
}

// ── Обработка входящих данных от SIM900 ────────────────

void processSIMData() {
    while (simSerial.available()) {
        char c = simSerial.read();
        if (c == '\n') {
            simBuffer.trim();
            if (simBuffer.length() > 0) {
                Serial.printf("[SIM] >> %s\n", simBuffer.c_str());

                // +CMTI: "SM",3  — новая SMS на SIM с индексом 3
                if (simBuffer.startsWith("+CMTI:")) {
                    int comma = simBuffer.indexOf(',');
                    if (comma != -1) {
                        int index = simBuffer.substring(comma + 1).toInt();
                        Serial.printf("[SMS] New SMS at index %d\n", index);
                        readAndForward(index);
                    }
                }
            }
            simBuffer = "";
        } else if (c != '\r') {
            simBuffer += c;
        }
    }
}

// ── Опрос SIM на неотправленные SMS ────────────────────

// Запрашивает список всех SMS на SIM (AT+CMGL=4 = все в PDU режиме).
// Для каждой пытается переслать и удалить.
void pollPendingSMS() {
    String resp = sendAT("AT+CMGL=4", 10000); // 4 = "ALL" в PDU режиме

    smsPending = 0;

    // Ответ — набор блоков +CMGL: <index>,<stat>,<alpha>,<length>\r\n<pdu>\r\n
    int searchFrom = 0;
    while (true) {
        int pos = resp.indexOf("+CMGL:", searchFrom);
        if (pos == -1) break;

        // Извлечь index
        int comma = resp.indexOf(',', pos);
        if (comma == -1) break;

        String indexStr = resp.substring(pos + 7, comma);
        indexStr.trim();
        int index = indexStr.toInt();

        // Найти PDU
        int lineEnd = resp.indexOf('\n', pos);
        if (lineEnd == -1) break;

        int pduStart = lineEnd + 1;
        int pduEnd = resp.indexOf('\r', pduStart);
        if (pduEnd == -1) pduEnd = resp.indexOf('\n', pduStart);
        if (pduEnd == -1) break;

        String pdu = resp.substring(pduStart, pduEnd);
        pdu.trim();

        if (pdu.length() >= 10) {
            Serial.printf("[POLL] SMS at index %d\n", index);
            if (forwardPDU(pdu, index)) {
                sendATok("AT+CMGD=" + String(index));
            } else {
                smsPending++;
            }
        }

        searchFrom = pduEnd + 1;
    }
}

// ── HTTP-ручки ─────────────────────────────────────────

void handleReboot() {
    server.send(200, "text/plain", "Rebooting...");
    delay(500);
    ESP.restart();
}

void handleResetModem() {
    Serial.println("[HTTP] /reset-modem: power cycling modem...");
    server.send(200, "text/plain", "Modem reset started...");

    sim900Ok = false;

    // Выключить питание
    digitalWrite(SIM_POWER_PIN, LOW);
    Serial.println("[SIM] Power OFF");
    delay(2000);

    // Включить питание
    digitalWrite(SIM_POWER_PIN, HIGH);
    Serial.println("[SIM] Power ON");
    delay(3000);

    // Переинициализировать
    while (simSerial.available()) simSerial.read();
    if (initSIM900()) {
        Serial.println("[SIM] Reset OK");
        pollPendingSMS();
    } else {
        Serial.println("[SIM] Reset FAILED");
        lastError = "Modem reset failed";
    }
}

void handleStatus() {
    JsonDocument doc;
    doc["uptime_sec"]    = (millis() - startTime) / 1000;
    doc["wifi_rssi"]     = WiFi.RSSI();
    doc["wifi_ip"]       = WiFi.localIP().toString();
    doc["sms_forwarded"] = smsForwarded;
    doc["sms_pending"]   = smsPending;
    doc["last_error"]    = lastError;
    doc["free_heap"]     = ESP.getFreeHeap();
    doc["sim900_ok"]     = sim900Ok;

    String body;
    serializeJson(doc, body);
    server.send(200, "application/json", body);
}

// ── Setup / Loop ───────────────────────────────────────

void setup() {
    startTime = millis();

    Serial.begin(115200);
    Serial.println("\n=== SMS Gateway starting ===");

    // Watchdog
    esp_task_wdt_init(WDT_TIMEOUT_SEC, true);
    esp_task_wdt_add(NULL);

    // Wi-Fi
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    ensureWiFi();

    // MQTT
    mqtt.setServer(MQTT_BROKER, MQTT_PORT);
    mqtt.setBufferSize(1024);
    ensureMQTT();

    // HTTP-сервер
    server.on("/reboot", handleReboot);
    server.on("/status", handleStatus);
    server.on("/reset-modem", handleResetModem);
    server.begin();
    Serial.println("[HTTP] Server started on port 80");

    // SIM900 — включить питание модема
    pinMode(SIM_POWER_PIN, OUTPUT);
    digitalWrite(SIM_POWER_PIN, HIGH);
    Serial.println("[SIM] Power ON (D4 HIGH)");
    delay(3000);  // дать модему время запуститься

    simSerial.setRxBufferSize(1024);
    simSerial.begin(SIM_BAUD, SERIAL_8N1, SIM_RX_PIN, SIM_TX_PIN);
    if (!initSIM900()) {
        Serial.println("[SIM] Init failed, will reboot in 10s");
        delay(10000);
        ESP.restart();
    }

    // Проверить SMS оставшиеся на SIM с прошлого раза
    pollPendingSMS();

    Serial.println("=== SMS Gateway ready ===");
}

void loop() {
    esp_task_wdt_reset();

    server.handleClient();
    ensureWiFi();
    mqtt.loop();
    processSIMData();

    // Периодический опрос неотправленных SMS
    if (millis() - lastPollMs > POLL_INTERVAL_MS) {
        lastPollMs = millis();
        pollPendingSMS();
    }
}
