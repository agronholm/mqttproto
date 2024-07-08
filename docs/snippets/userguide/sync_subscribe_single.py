from __future__ import annotations

from mqttproto.sync_client import MQTTClient

with MQTTClient() as client:
    with client.subscribe("topic") as sub:
        for message in sub:
            print(f"Received a message: {message.payload!r}")
