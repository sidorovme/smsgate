#include <Arduino.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <esp_task_wdt.h>
#include <vector>
#include <stdarg.h>
#include "config.h"
#include "topics.h"
#include "display.h"

// ── Глобальные переменные ──────────────────────────────
HardwareSerial simSerial(2);
WiFiClient wifiClient;
PubSubClient mqtt(wifiClient);

uint32_t smsForwarded  = 0;
uint32_t smsSent       = 0;
uint32_t smsPending    = 0;
String   lastError     = "";
bool     sim900Ok      = false;
uint32_t startTime     = 0;
uint32_t lastPollMs    = 0;
uint32_t lastDisplayMs = 0;
uint32_t lastStatusMs  = 0;
uint32_t lastMqttMs    = 0;   // троттлинг попыток реконнекта к брокеру
String   simBuffer     = "";

// Состояние сотовой сети (AT+CREG? / AT+COPS? / AT+CSQ)
#ifndef NET_CHECK_MS
#define NET_CHECK_MS 15000
#endif
int      netStat       = -1;   // CREG stat: 0 нет, 1 home, 2 поиск, 3 отказ, 4 ?, 5 roaming; -1 неизвестно
String   netOperator   = "";
int      netCsq        = 99;   // 0..31, 99 = неизвестно
uint32_t lastNetMs     = 0;
uint8_t  netNoReply    = 0;    // подряд неотвеченных CREG? (модем завис?)
uint32_t lastAutoResetMs = 0;  // последний автоматический power cycle модема

// Команды из MQTT выполняются в loop(), а не в callback (тяжёлая работа
// внутри колбэка PubSubClient нежелательна).
enum PendingCmd { CMD_NONE, CMD_REBOOT, CMD_RESET_MODEM };
volatile PendingCmd pendingCmd = CMD_NONE;

// Очередь заданий на отправку SMS из MQTT. Тяжёлая работа (AT+CMGS, до минуты)
// выполняется в loop(), callback только разбирает JSON и кладёт задание в очередь.
struct SendPart { String pdu; int len; };
struct SendJob {
    long id = 0;
    String error;                 // если не пусто — задание отклонено сразу
    std::vector<SendPart> parts;
};
std::vector<SendJob> sendQueue;
const size_t SEND_QUEUE_MAX = 4;
long recentSendIds[8] = {0};      // дедупликация повторной доставки MQTT (QoS 1)
uint8_t recentSendPos = 0;
struct UssdJob {
    long id = 0;
    String code;
    bool cancel = false;
};
std::vector<UssdJob> ussdQueue;
const size_t USSD_QUEUE_MAX = 2;
long recentUssdIds[8] = {0};
uint8_t recentUssdPos = 0;
struct FwdJob {
    long id = 0;
    String action;   // "query" | "set" | "off"
    String number;
    int reason = 0;  // 0 все, 1 занято, 2 нет ответа, 3 вне зоны, 4 все условия, 5 все условные
};
std::vector<FwdJob> fwdQueue;
const size_t FWD_QUEUE_MAX = 2;
long recentFwdIds[8] = {0};
uint8_t recentFwdPos = 0;
bool expectCdsPdu = false;        // после "+CDS: <len>" следующая строка — PDU отчёта

// ── Диагностика в MQTT ─────────────────────────────────
// Строки идут в Serial и в MQTT_DEBUG_TOPIC. Пока брокер недоступен, последние
// DBG_BUF_N строк копятся и уходят после переподключения.
#define DBG_BUF_N 16
String   dbgBuf[DBG_BUF_N];
uint32_t dbgHead  = 0;
uint8_t  dbgCount = 0;

void dbg(const char* fmt, ...) {
    char line[200];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(line, sizeof(line), fmt, ap);
    va_end(ap);

    char out[240];
    snprintf(out, sizeof(out), "[%lus] %s", (unsigned long)((millis() - startTime) / 1000), line);
    Serial.printf("[DBG] %s\n", out);

    if (mqtt.connected()) {
        mqtt.publish(MQTT_DEBUG_TOPIC, out);
    } else {
        dbgBuf[dbgHead++ % DBG_BUF_N] = out;
        if (dbgCount < DBG_BUF_N) dbgCount++;
    }
}

// Отправить накопленные строки (вызывается сразу после подключения к брокеру).
void flushDbg() {
    uint32_t first = dbgCount < DBG_BUF_N ? 0 : dbgHead;
    for (uint8_t i = 0; i < dbgCount; i++) {
        mqtt.publish(MQTT_DEBUG_TOPIC, dbgBuf[(first + i) % DBG_BUF_N].c_str());
    }
    dbgCount = 0;
    dbgHead = 0;
}

// Ответ модема в одну строку для лога: переводы строк → "|".
String oneLine(String s) {
    s.replace("\r", "");
    s.trim();
    s.replace("\n", "|");
    return s;
}

// ── Предварительные объявления ─────────────────────────
void publishStatus();
void handleSimLine(const String& line);

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
    sendATok("AT+CSCS=\"GSM\""); // текст USSD в GSM-кодировке (UCS-2 ответы придут в hex)
    sendATok("AT+CREG=1");         // URC +CREG: <stat> при смене регистрации (диагностика)
    sendATok("AT+CNMI=2,1,0,1,0"); // +CMTI при новой SMS, +CDS (отчёт о доставке) напрямую

    Serial.println("[SIM] Ready");
    return true;
}

// ── MQTT подключение ─────────────────────────────────

void ensureMQTT() {
    if (mqtt.connected()) return;
    if (WiFi.status() != WL_CONNECTED) return;

    // Не долбить брокер каждым тиком loop() — попытка connect() блокирующая.
    if (lastMqttMs != 0 && millis() - lastMqttMs < MQTT_RETRY_MS) return;
    lastMqttMs = millis();

    Serial.printf("[MQTT] Connecting to %s:%d...\n", MQTT_BROKER, MQTT_PORT);
    // LWT: при неожиданном обрыве брокер сам опубликует "offline" (retained).
    if (mqtt.connect(DEVICE_ID, MQTT_AVAILABILITY_TOPIC, 0, true, "offline")) {
        Serial.println("[MQTT] Connected");
        mqtt.publish(MQTT_AVAILABILITY_TOPIC, "online", true); // birth-сообщение
        mqtt.subscribe(MQTT_CMD_TOPIC);
        mqtt.subscribe(MQTT_SEND_TOPIC, 1);
        mqtt.subscribe(MQTT_USSD_TOPIC, 1);
        mqtt.subscribe(MQTT_FORWARD_TOPIC, 1);
        flushDbg();
        dbg("MQTT connected, wifi_rssi=%d", WiFi.RSSI());
        Serial.printf("[MQTT] Subscribed to %s, %s\n", MQTT_CMD_TOPIC, MQTT_SEND_TOPIC);
        publishStatus();
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

// Обработка одной строки от модема (URC): +CMTI — новая SMS, +CDS — отчёт о доставке.
void handleSimLine(const String& line) {
    Serial.printf("[SIM] >> %s\n", line.c_str());

    // +CMTI: "SM",3  — новая SMS на SIM с индексом 3
    if (line.startsWith("+CMTI:")) {
        int comma = line.indexOf(',');
        if (comma != -1) {
            int index = line.substring(comma + 1).toInt();
            Serial.printf("[SMS] New SMS at index %d\n", index);
            readAndForward(index);
        }
        return;
    }

    // +CDS: <len>\r\n<pdu> — отчёт о доставке (ds=1 в CNMI)
    if (line.startsWith("+CDS:")) {
        expectCdsPdu = true;
        return;
    }

    // Остальные URC (+CREG, UNDER-VOLTAGE, RDY, Call Ready...) — в диагностику.
    if (!expectCdsPdu) {
        dbg("URC: %s", line.c_str());
        if (line.indexOf("VOLTAGE") != -1 || line.indexOf("POWER DOWN") != -1) {
            lastError = "Modem: " + line;
        }
    }
    if (expectCdsPdu && line.length() >= 10) {
        expectCdsPdu = false;
        if (!mqtt.connected()) {
            Serial.println("[CDS] MQTT disconnected, delivery report lost");
            return;
        }
        JsonDocument doc;
        doc["pdu"]       = line;
        doc["device_id"] = DEVICE_ID;
        String body;
        serializeJson(doc, body);
        mqtt.publish(MQTT_REPORT_TOPIC, body.c_str());
        Serial.println("[CDS] Delivery report forwarded");
    }
}

void processSIMData() {
    while (simSerial.available()) {
        char c = simSerial.read();
        if (c == '\n') {
            simBuffer.trim();
            if (simBuffer.length() > 0) handleSimLine(simBuffer);
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

// ── Состояние сотовой сети ─────────────────────────────

// Опрашивает регистрацию в сети, оператора и уровень сигнала.
// Возвращает true, если регистрация/оператор изменились.
bool updateNetwork() {
    int oldStat = netStat;
    String oldOp = netOperator;

    String r = sendAT("AT+CREG?");
    int p = r.indexOf("+CREG:");
    int comma = p == -1 ? -1 : r.indexOf(',', p);
    if (comma == -1) {
        // Нет ответа / не разобрали: состояние не трогаем (это не «сеть пропала»),
        // но считаем подряд идущие сбои — молчащий модем перезапустит loop().
        netNoReply++;
        dbg("CREG? PARSE FAIL (%d in a row): %s", netNoReply, oneLine(r).c_str());
        return false;
    }
    netNoReply = 0;
    netStat = r.substring(comma + 1).toInt();

    if (netStat == 1 || netStat == 5) {
        String o = sendAT("AT+COPS?");
        int q1 = o.indexOf('"');
        int q2 = q1 == -1 ? -1 : o.indexOf('"', q1 + 1);
        if (q2 != -1) netOperator = o.substring(q1 + 1, q2);
        else dbg("COPS? PARSE FAIL: %s", oneLine(o).c_str());  // прежний оператор остаётся
    } else {
        netOperator = "";
    }

    // CSQ читаем всегда (и без регистрации) — для корреляции потерь сети с уровнем сигнала
    String c = sendAT("AT+CSQ");
    int cp = c.indexOf("+CSQ:");
    if (cp != -1) netCsq = c.substring(cp + 5).toInt();
    else dbg("CSQ PARSE FAIL: %s", oneLine(c).c_str());

    bool changed = netStat != oldStat || netOperator != oldOp;
    static uint8_t beat = 0;
    if (changed || ++beat >= 20) {  // при изменении и раз в ~5 минут
        beat = 0;
        dbg("NET stat=%d(was %d) op='%s' csq=%d | CREG=%s",
            netStat, oldStat, netOperator.c_str(), netCsq, oneLine(r).c_str());
    }
    return changed;
}

// ── Отправка SMS ───────────────────────────────────────

// Ждёт в буфере buf подстроку(и) из условия, периодически кормит watchdog и MQTT.
// Возвращает true, если условие выполнено до таймаута.
template <typename Cond>
bool waitModem(String& buf, uint32_t timeoutMs, Cond done) {
    uint32_t start = millis();
    while (millis() - start < timeoutMs) {
        esp_task_wdt_reset();
        while (simSerial.available()) buf += (char)simSerial.read();
        if (done(buf)) return true;
        mqtt.loop(); // keep-alive во время долгого ожидания
        delay(10);
    }
    return false;
}

// Отправляет один PDU командой AT+CMGS. При успехе возвращает true и message reference.
bool sendPdu(const SendPart& part, int& mr, String& err, String& extra) {
    processSIMData();  // разобрать накопившиеся URC до начала диалога с модемом

    String resp;
    simSerial.printf("AT+CMGS=%d\r", part.len);
    bool prompt = waitModem(resp, 5000, [](const String& b) {
        return b.indexOf('>') != -1 || b.indexOf("ERROR") != -1;
    });
    if (!prompt || resp.indexOf('>') == -1) {
        if (!prompt) simSerial.write(0x1B); // ESC — отмена ввода
        err = prompt ? "modem rejected CMGS" : "no CMGS prompt";
        extra += resp;
        return false;
    }

    simSerial.print(part.pdu);
    simSerial.write(0x1A); // Ctrl+Z

    String result;
    bool done = waitModem(result, 60000, [](const String& b) {
        return (b.indexOf("+CMGS:") != -1 && b.indexOf("OK") != -1) || b.indexOf("ERROR") != -1;
    });
    Serial.printf("[SEND] CMGS → %s\n", result.c_str());
    extra += resp + result;

    int p = result.indexOf("+CMGS:");
    if (done && p != -1) {
        mr = result.substring(p + 6).toInt();
        return true;
    }

    int e = result.indexOf("ERROR");
    if (e != -1) {
        int eol = result.indexOf('\r', e);
        err = result.substring(result.lastIndexOf('+', e), eol == -1 ? result.length() : eol);
        err.trim();
    } else {
        err = "send timeout";
    }
    return false;
}

void publishSendResult(long id, bool ok, const std::vector<int>& refs, const String& err) {
    if (!mqtt.connected()) {
        Serial.println("[SEND] MQTT disconnected, result not published");
        return;
    }
    JsonDocument doc;
    doc["id"]     = id;
    doc["status"] = ok ? "ok" : "error";
    JsonArray arr = doc["refs"].to<JsonArray>();
    for (int r : refs) arr.add(r);
    if (!ok) doc["error"] = err;

    String body;
    serializeJson(doc, body);
    mqtt.publish(MQTT_SEND_RESULT_TOPIC, body.c_str());
}

void processSendJob(const SendJob& job) {
    Serial.printf("[SEND] Job %ld: %u part(s)\n", job.id, (unsigned)job.parts.size());

    std::vector<int> refs;
    String err = job.error;

    if (err.length() == 0) {
        if (!sim900Ok) err = "modem not ready";
        else if (netStat != 1 && netStat != 5) err = "no network";
    }

    String extra;
    if (err.length() == 0) {
        for (const SendPart& part : job.parts) {
            int mr = -1;
            if (!sendPdu(part, mr, err, extra)) break;
            refs.push_back(mr);
            smsSent++;
        }
    }

    bool ok = err.length() == 0;
    if (!ok) {
        lastError = "SMS send: " + err;
        Serial.printf("[SEND] Job %ld FAILED: %s\n", job.id, err.c_str());
    }
    publishSendResult(job.id, ok, refs, err);

    // URC, пришедшие во время диалога (+CMTI, +CDS), не теряем
    int from = 0;
    while (from < (int)extra.length()) {
        int nl = extra.indexOf('\n', from);
        if (nl == -1) nl = extra.length();
        String line = extra.substring(from, nl);
        line.trim();
        from = nl + 1;
        if (line.length() == 0 || line == "OK" || line == ">" || line.startsWith("+CMGS:") ||
            line.indexOf("ERROR") != -1) continue;
        handleSimLine(line);
    }
    publishStatus();
}

// Разбор JSON-задания из MQTT: {"id":N,"to":"...","parts":[{"pdu":"..","len":N},...]}
void enqueueSend(const byte* payload, unsigned int length) {
    JsonDocument doc;
    if (deserializeJson(doc, payload, length)) {
        Serial.println("[SEND] Bad JSON in send command");
        return;
    }
    SendJob job;
    job.id = doc["id"] | 0L;
    if (job.id == 0) return;

    for (long seen : recentSendIds) {
        if (seen == job.id) {
            Serial.printf("[SEND] Duplicate job %ld ignored\n", job.id);
            return;
        }
    }
    recentSendIds[recentSendPos++ % 8] = job.id;

    for (JsonObject p : doc["parts"].as<JsonArray>()) {
        SendPart part;
        part.pdu = p["pdu"].as<String>();
        part.len = p["len"] | 0;
        if (part.pdu.length() == 0 || part.len <= 0) { job.error = "bad part"; break; }
        job.parts.push_back(part);
    }
    if (job.parts.empty() && job.error.length() == 0) job.error = "no parts";
    if (sendQueue.size() >= SEND_QUEUE_MAX) job.error = "send queue full";

    sendQueue.push_back(job);
}

// ── USSD ───────────────────────────────────────────────

// Разбирает строку URC "+CUSD: <n>[,"<text>",<dcs>]" из буфера.
// Возвращает true, когда строка пришла целиком (текст может содержать переводы строк).
bool parseCusd(const String& buf, int& n, String& text, int& dcs) {
    int p = buf.indexOf("+CUSD:");
    if (p == -1) return false;
    int eol = buf.indexOf('\n', p);
    int q1 = buf.indexOf('"', p);

    if (q1 == -1 || (eol != -1 && eol < q1)) {  // без текста, например "+CUSD: 2"
        if (eol == -1) return false;
        n = buf.substring(p + 6).toInt();
        text = "";
        dcs = 0;
        return true;
    }

    int q2 = buf.lastIndexOf('"');
    if (q2 <= q1) return false;
    int tail = buf.indexOf('\n', q2);
    if (tail == -1) return false;

    n = buf.substring(p + 6).toInt();
    text = buf.substring(q1 + 1, q2);
    int comma = buf.indexOf(',', q2);
    dcs = (comma != -1 && comma < tail) ? buf.substring(comma + 1).toInt() : 0;
    return true;
}

void publishUssdResult(long id, bool ok, int n, const String& text, int dcs, const String& err) {
    if (!mqtt.connected()) {
        Serial.println("[USSD] MQTT disconnected, result not published");
        return;
    }
    JsonDocument doc;
    doc["id"]     = id;
    doc["status"] = ok ? "ok" : "error";
    if (ok) {
        doc["n"]    = n;
        doc["text"] = text;
        doc["dcs"]  = dcs;
    } else {
        doc["error"] = err;
    }
    String body;
    serializeJson(doc, body);
    mqtt.publish(MQTT_USSD_RESULT_TOPIC, body.c_str());
}

void processUssdJob(const UssdJob& job) {
    Serial.printf("[USSD] Job %ld: %s\n", job.id, job.cancel ? "cancel" : job.code.c_str());

    String err;
    if (!sim900Ok) err = "modem not ready";
    else if (!job.cancel && netStat != 1 && netStat != 5) err = "no network";

    if (err.length() > 0) {
        publishUssdResult(job.id, false, 0, "", 0, err);
        return;
    }

    processSIMData();  // разобрать накопившиеся URC до начала диалога

    String cmd = job.cancel ? String("AT+CUSD=2") : "AT+CUSD=1,\"" + job.code + "\",15";
    simSerial.println(cmd);

    // Сначала приходит OK, затем (через 2-30 с) URC +CUSD с ответом оператора.
    String buf;
    int n = 0, dcs = 0;
    String text;
    bool done = waitModem(buf, 35000, [&](const String& b) {
        return b.indexOf("ERROR") != -1 || parseCusd(b, n, text, dcs);
    });
    Serial.printf("[USSD] ← %s\n", buf.c_str());
    dbg("USSD %s -> %s", job.cancel ? "cancel" : job.code.c_str(), oneLine(buf).c_str());

    if (done && buf.indexOf("+CUSD:") != -1) {
        publishUssdResult(job.id, true, n, text, dcs, "");
    } else if (buf.indexOf("ERROR") != -1) {
        int e = buf.indexOf("ERROR");
        int eol = buf.indexOf('\r', e);
        String line = buf.substring(buf.lastIndexOf('+', e), eol == -1 ? buf.length() : eol);
        line.trim();
        publishUssdResult(job.id, false, 0, "", 0, line);
    } else if (job.cancel && buf.indexOf("OK") != -1) {
        publishUssdResult(job.id, true, 2, "", 0, "");  // сессия закрыта, +CUSD не будет
    } else {
        publishUssdResult(job.id, false, 0, "", 0, "ussd timeout");
    }
}

// Разбор JSON-задания: {"id":N,"code":"*101#"} или {"id":N,"cancel":true}
void enqueueUssd(const byte* payload, unsigned int length) {
    JsonDocument doc;
    if (deserializeJson(doc, payload, length)) {
        Serial.println("[USSD] Bad JSON");
        return;
    }
    UssdJob job;
    job.id = doc["id"] | 0L;
    if (job.id == 0) return;

    for (long seen : recentUssdIds) {
        if (seen == job.id) return;  // повторная доставка MQTT
    }
    recentUssdIds[recentUssdPos++ % 8] = job.id;

    job.cancel = doc["cancel"] | false;
    job.code = doc["code"].as<String>();
    if (!job.cancel && job.code.length() == 0) return;
    if (ussdQueue.size() >= USSD_QUEUE_MAX) return;  // бэкенд отвалится по таймауту
    ussdQueue.push_back(job);
}

// ── Переадресация звонков (AT+CCFC) ────────────────────

// AT-команда с долгим ожиданием (сеть отвечает до ~30 с), без потери keep-alive MQTT.
String atWait(const String& cmd, uint32_t timeoutMs, bool& ok, const char* tag = "FWD") {
    processSIMData();
    simSerial.println(cmd);
    String buf;
    waitModem(buf, timeoutMs, [](const String& b) {
        return b.indexOf("OK") != -1 || b.indexOf("ERROR") != -1;
    });
    ok = buf.indexOf("OK") != -1 && buf.indexOf("ERROR") == -1;
    Serial.printf("[FWD] %s → %s\n", cmd.c_str(), buf.c_str());
    dbg("%s %s -> %s", tag, cmd.c_str(), oneLine(buf).c_str());
    return buf;
}

// Условия переадресации: 0 все вызовы, 1 занято, 2 нет ответа, 3 вне зоны.
void queryForward(int reason, JsonArray& out) {
    bool ok;
    String r = atWait("AT+CCFC=" + String(reason) + ",2", 30000, ok);
    JsonObject o = out.add<JsonObject>();
    o["reason"] = reason;
    if (!ok) {
        int e = r.indexOf("ERROR");
        String err = e == -1 ? String("timeout") : r.substring(r.lastIndexOf('+', e), r.indexOf('\r', e) == -1 ? r.length() : r.indexOf('\r', e));
        err.trim();
        o["error"] = err;
        return;
    }
    // +CCFC: <status>,<class>[,"<number>",<type>] — по строке на класс; нас интересует голос (class & 1)
    bool active = false;
    String number = "";
    int from = 0;
    while (true) {
        int p = r.indexOf("+CCFC:", from);
        if (p == -1) break;
        int eol = r.indexOf('\n', p);
        if (eol == -1) eol = r.length();
        String line = r.substring(p + 6, eol);
        int status = line.toInt();
        int comma = line.indexOf(',');
        int cls = comma == -1 ? 1 : line.substring(comma + 1).toInt();
        if (status == 1 && (cls & 1)) {
            active = true;
            int q1 = line.indexOf('"');
            int q2 = q1 == -1 ? -1 : line.indexOf('"', q1 + 1);
            if (q2 != -1) number = line.substring(q1 + 1, q2);
        }
        from = eol;
    }
    o["active"] = active;
    if (active) o["number"] = number;
}

void processFwdJob(const FwdJob& job) {
    Serial.printf("[FWD] Job %ld: %s %s\n", job.id, job.action.c_str(), job.number.c_str());

    String err;
    if (!sim900Ok) err = "modem not ready";
    else if (netStat != 1 && netStat != 5) err = "no network";

    bool ok = true;
    if (err.length() == 0 && job.action == "set") {
        // 145 — международный формат ("+" оставляем: без него SIM900 отвечает
        // "operation not allowed" ещё до обращения к сети), 129 — как есть.
        int type = job.number.startsWith("+") ? 145 : 129;
        atWait("AT+CCFC=" + String(job.reason) + ",3,\"" + job.number + "\"," + String(type) + ",1", 30000, ok);  // class 1 = только голос
        if (!ok) err = "forward set failed";
    } else if (err.length() == 0 && job.action == "off") {
        atWait("AT+CCFC=" + String(job.reason) + ",4", 30000, ok);  // mode 4 = стереть
        if (!ok) err = "forward off failed";
    }

    JsonDocument doc;
    doc["id"] = job.id;
    if (err.length() > 0) {
        doc["status"] = "error";
        doc["error"]  = err;
    } else {
        doc["status"] = "ok";
        JsonArray arr = doc["forwards"].to<JsonArray>();
        for (int reason = 0; reason <= 3; reason++) {
            esp_task_wdt_reset();
            queryForward(reason, arr);
        }
    }

    if (mqtt.connected()) {
        String body;
        serializeJson(doc, body);
        mqtt.publish(MQTT_FORWARD_RESULT_TOPIC, body.c_str());
    }
}

// {"id":N,"action":"query|set|off","number":"+..."}
void enqueueFwd(const byte* payload, unsigned int length) {
    JsonDocument doc;
    if (deserializeJson(doc, payload, length)) return;

    FwdJob job;
    job.id = doc["id"] | 0L;
    if (job.id == 0) return;
    for (long seen : recentFwdIds) {
        if (seen == job.id) return;
    }
    recentFwdIds[recentFwdPos++ % 8] = job.id;

    job.action = doc["action"].as<String>();
    job.number = doc["number"].as<String>();
    job.reason = doc["reason"] | (job.action == "off" ? 4 : 0);
    if (job.reason < 0 || job.reason > 5 || (job.action == "set" && job.reason == 4)) return;
    if (job.action != "query" && job.action != "off" && job.action != "set") return;
    if (job.action == "set") {
        // номер подставляется в AT-команду — пропускаем только + и цифры
        if (job.number.length() < 5 || job.number.length() > 20) return;
        for (unsigned int i = 0; i < job.number.length(); i++) {
            char c = job.number[i];
            if (!(isdigit(c) || (i == 0 && c == '+'))) return;
        }
    }
    if (fwdQueue.size() >= FWD_QUEUE_MAX) return;
    fwdQueue.push_back(job);
}

// ── Статус и команды через MQTT ────────────────────────

// Публикует метрики устройства в MQTT_STATUS_TOPIC (retained).
void publishStatus() {
    if (!mqtt.connected()) return;

    JsonDocument doc;
    doc["uptime_sec"]    = (millis() - startTime) / 1000;
    doc["wifi_rssi"]     = WiFi.RSSI();
    doc["wifi_ip"]       = WiFi.localIP().toString();
    doc["sms_forwarded"] = smsForwarded;
    doc["sms_sent"]      = smsSent;
    doc["sms_pending"]   = smsPending;
    doc["last_error"]    = lastError;
    doc["free_heap"]     = ESP.getFreeHeap();
    doc["sim900_ok"]     = sim900Ok;
    doc["net_stat"]      = netStat;
    doc["net_registered"] = (netStat == 1 || netStat == 5);
    doc["net_roaming"]   = (netStat == 5);
    doc["net_operator"]  = netOperator;
    doc["net_csq"]       = netCsq;

    String body;
    serializeJson(doc, body);
    mqtt.publish(MQTT_STATUS_TOPIC, body.c_str(), true); // retained
}

// Перезагрузка ESP32 (команда reboot).
void doReboot() {
    Serial.println("[CMD] Reboot requested");
    if (mqtt.connected()) {
        mqtt.publish(MQTT_AVAILABILITY_TOPIC, "offline", true);
        mqtt.loop(); // дать библиотеке отправить сообщение перед рестартом
    }
    delay(200);
    ESP.restart();
}

// Power cycle модема SIM900 и переинициализация (команда reset-modem).
void resetModem() {
    Serial.println("[CMD] reset-modem: power cycling modem...");

    sim900Ok = false;
    netStat = -1;
    netOperator = "";

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
        publishStatus();
        // Модем не поднялся: перезагружаем плату (при загрузке снова power cycle),
        // иначе sim900Ok остался бы false и опрос сети больше не запускался бы.
        Serial.println("[SIM] Rebooting in 10s");
        delay(10000);
        ESP.restart();
    }

    publishStatus();
}

// Callback входящих MQTT-сообщений на MQTT_CMD_TOPIC.
// Только помечает команду; выполнение — в loop().
void onMqttMessage(char* topic, byte* payload, unsigned int length) {
    if (strcmp(topic, MQTT_SEND_TOPIC) == 0) {
        enqueueSend(payload, length);
        return;
    }
    if (strcmp(topic, MQTT_FORWARD_TOPIC) == 0) {
        enqueueFwd(payload, length);
        return;
    }
    if (strcmp(topic, MQTT_USSD_TOPIC) == 0) {
        enqueueUssd(payload, length);
        return;
    }

    String cmd;
    cmd.reserve(length);
    for (unsigned int i = 0; i < length; i++) cmd += (char)payload[i];
    cmd.trim();

    Serial.printf("[MQTT] Command on %s: %s\n", topic, cmd.c_str());

    if (cmd == "reboot") {
        pendingCmd = CMD_REBOOT;
    } else if (cmd == "reset-modem") {
        pendingCmd = CMD_RESET_MODEM;
    } else if (cmd == "status") {
        publishStatus();
    } else {
        Serial.printf("[MQTT] Unknown command: %s\n", cmd.c_str());
    }
}

// ── Setup / Loop ───────────────────────────────────────

void setup() {
    startTime = millis();

    Serial.begin(115200);
    Serial.println("\n=== SMS Gateway starting ===");

    // OLED — инициализация первым, чтобы показывать прогресс загрузки
    displayInit();

    // Watchdog
    esp_task_wdt_init(WDT_TIMEOUT_SEC, true);
    esp_task_wdt_add(NULL);

    // Wi-Fi
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    ensureWiFi();
    displayBoot("WiFi", WiFi.status() == WL_CONNECTED);

    // MQTT
    mqtt.setServer(MQTT_BROKER, MQTT_PORT);
    mqtt.setBufferSize(4096); // PDU входящих + задания на отправку до нескольких частей
    mqtt.setCallback(onMqttMessage);
    ensureMQTT();
    displayBoot("MQTT", mqtt.connected());

    // SIM900 — включить питание модема
    // Всегда с power cycle: после программной перезагрузки ESP32 зависший модем
    // иначе остаётся без сброса и не отвечает на AT (цикл перезагрузок).
    pinMode(SIM_POWER_PIN, OUTPUT);
    digitalWrite(SIM_POWER_PIN, LOW);
    Serial.println("[SIM] Power OFF");
    delay(2000);
    digitalWrite(SIM_POWER_PIN, HIGH);
    Serial.println("[SIM] Power ON (D4 HIGH)");
    delay(3000);  // дать модему время запуститься

    simSerial.setRxBufferSize(1024);
    simSerial.begin(SIM_BAUD, SERIAL_8N1, SIM_RX_PIN, SIM_TX_PIN);
    if (!initSIM900()) {
        displayBoot("SIM900", false);
        Serial.println("[SIM] Init failed, will reboot in 10s");
        delay(10000);
        ESP.restart();
    }
    displayBoot("SIM900", true);

    // Проверить SMS оставшиеся на SIM с прошлого раза
    pollPendingSMS();
    updateNetwork();
    publishStatus();

    displayBootReady();
    Serial.println("=== SMS Gateway ready ===");
    delay(2000);  // показать boot-экран 2 секунды перед переходом к статусу
}

void loop() {
    esp_task_wdt_reset();

    ensureWiFi();
    ensureMQTT();
    mqtt.loop();
    processSIMData();

    // Отложенные команды из MQTT
    if (pendingCmd == CMD_REBOOT) {
        pendingCmd = CMD_NONE;
        doReboot();
    } else if (pendingCmd == CMD_RESET_MODEM) {
        pendingCmd = CMD_NONE;
        resetModem();
    }

    // Задания на отправку SMS (по одному за итерацию)
    if (!sendQueue.empty()) {
        SendJob job = sendQueue.front();
        sendQueue.erase(sendQueue.begin());
        processSendJob(job);
    }

    // Переадресация звонков
    if (!fwdQueue.empty()) {
        FwdJob job = fwdQueue.front();
        fwdQueue.erase(fwdQueue.begin());
        processFwdJob(job);
    }

    // USSD-запросы
    if (!ussdQueue.empty()) {
        UssdJob job = ussdQueue.front();
        ussdQueue.erase(ussdQueue.begin());
        processUssdJob(job);
    }

    // Периодический опрос неотправленных SMS
    if (millis() - lastPollMs > POLL_INTERVAL_MS) {
        lastPollMs = millis();
        pollPendingSMS();
    }

    // Состояние сотовой сети; при изменении — сразу публикуем статус
    if (sim900Ok && millis() - lastNetMs > NET_CHECK_MS) {
        lastNetMs = millis();
        if (updateNetwork()) publishStatus();

        // Модем молчит на AT — перезапускаем по питанию (не чаще раза в 5 минут)
        if (netNoReply >= 3 && (lastAutoResetMs == 0 || millis() - lastAutoResetMs > 300000)) {
            dbg("Modem not responding (%d CREG? fails), auto reset", netNoReply);
            lastAutoResetMs = millis();
            netNoReply = 0;
            lastError = "Modem hung, auto reset";
            pendingCmd = CMD_RESET_MODEM;
        }
    }

    // Периодическая публикация статуса в MQTT
    if (millis() - lastStatusMs > STATUS_INTERVAL_MS) {
        lastStatusMs = millis();
        publishStatus();
    }

    // Обновление дисплея
    if (millis() - lastDisplayMs > DISPLAY_UPDATE_MS) {
        lastDisplayMs = millis();
        displayStatus();
    }
}
