#!/usr/bin/env bash
set -uo pipefail

MQTT_BROKER="${3:-127.0.0.1}"
MQTT_PORT="${4:-1883}"
MQTT_TOPIC="${5:-matter2mqtt}"

mosquitto_pub -h "$MQTT_BROKER" -p "$MQTT_PORT" -t "$MQTT_TOPIC/bridge/request/device/rename" -m '{"from":"'$1'", "to": "'$2'"}'

