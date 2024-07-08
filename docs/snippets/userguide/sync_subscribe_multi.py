from __future__ import annotations

from mqttproto.sync_client import MQTTClient

with MQTTClient() as client:
    with client.subscribe("topic", "more/+/topics", "other/#") as sub:
        for message in sub:
            print(f"Received a message from {message.topic}: {message.payload!r}")
