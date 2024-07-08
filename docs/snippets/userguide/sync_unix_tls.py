from __future__ import annotations

from mqttproto.sync_client import MQTTClient

with MQTTClient(
    host_or_path="/path/to/broker.sock", transport="unix", ssl=True
) as client:
    ...
