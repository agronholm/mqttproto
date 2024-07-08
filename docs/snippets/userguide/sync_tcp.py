from __future__ import annotations

from mqttproto.sync_client import MQTTClient

with MQTTClient(host_or_path="localhost") as client:
    ...
