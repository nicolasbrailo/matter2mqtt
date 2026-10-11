 mosquitto_sub -h localhost -t matter2mqtt/bridge/devices -C 1 | jq -r '.[].friendly_name'
