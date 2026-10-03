#pragma once

// Дерево MQTT-топиков шлюза. Задаётся одним параметром MQTT_BASE в config.h
// (например "sms/gate-yauheni"); backend строит такие же топики из mqtt_base в gateways.yaml.
#ifndef MQTT_BASE
#error "MQTT_BASE не задан в config.h (например \"sms/gate-1\")"
#endif

#define MQTT_TOPIC                MQTT_BASE "/incoming"        // входящие SMS (PDU)
#define MQTT_STATUS_TOPIC         MQTT_BASE "/status"          // метрики устройства (retained)
#define MQTT_AVAILABILITY_TOPIC   MQTT_BASE "/availability"    // online/offline (LWT, retained)
#define MQTT_CMD_TOPIC            MQTT_BASE "/cmd"             // команды: reboot, reset-modem, status
#define MQTT_SEND_TOPIC           MQTT_BASE "/send"            // задания на отправку SMS (JSON с PDU)
#define MQTT_SEND_RESULT_TOPIC    MQTT_BASE "/send-result"     // результат отправки
#define MQTT_REPORT_TOPIC         MQTT_BASE "/report"          // отчёты о доставке (PDU)
#define MQTT_USSD_TOPIC           MQTT_BASE "/ussd"            // USSD-запросы (JSON)
#define MQTT_USSD_RESULT_TOPIC    MQTT_BASE "/ussd-result"     // ответы на USSD
#define MQTT_FORWARD_TOPIC        MQTT_BASE "/forward"         // управление переадресацией звонков (JSON)
#define MQTT_FORWARD_RESULT_TOPIC MQTT_BASE "/forward-result"  // состояние переадресации
#define MQTT_DEBUG_TOPIC          MQTT_BASE "/debug"           // диагностические строки (не retained)
