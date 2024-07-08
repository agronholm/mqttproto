from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from mqttproto.sync_client import MQTTClient


def subscriber1(client: MQTTClient) -> None:
    with client.subscribe("more/+/topics") as sub:
        for message in sub:
            print(
                f"subscriber1: received a message from {message.topic}: "
                f"{message.payload!r}"
            )


def subscriber2(client: MQTTClient) -> None:
    with client.subscribe("other/#") as sub:
        for message in sub:
            print(
                f"subscriber2: received a message from {message.topic}: "
                f"{message.payload!r}"
            )


with MQTTClient() as client, ThreadPoolExecutor() as executor:
    executor.submit(subscriber1, client)
    executor.submit(subscriber2, client)
