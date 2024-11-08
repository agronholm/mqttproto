from __future__ import annotations

import pytest

from mqttproto import MQTTPublishPacket, QoS
from mqttproto.sync_client import MQTTClient

pytestmark = [pytest.mark.network]


@pytest.mark.parametrize("qos", [QoS.AT_MOST_ONCE, QoS.AT_LEAST_ONCE, QoS.EXACTLY_ONCE])
def test_publish_subscribe(qos: QoS) -> None:
    with MQTTClient() as client:
        with client.subscribe("test/+") as messages:
            client.publish("test/text", "test åäö", qos=qos)
            client.publish("test/binary", b"\x00\xff\x00\x1f", qos=qos)
            packets: list[MQTTPublishPacket] = []
            for packet in messages:
                packets.append(packet)
                if len(packets) == 2:
                    break

            assert packets[0].topic == "test/text"
            assert packets[0].payload == "test åäö"
            assert packets[1].topic == "test/binary"
            assert packets[1].payload == b"\x00\xff\x00\x1f"


def test_retained_message() -> None:
    try:
        with MQTTClient() as client:
            if not client.may_retain:
                pytest.skip("Retain not available")
            client.publish("retainedtest", "test åäö", retain=True)
            with client.subscribe("retainedtest") as messages:
                for packet in messages:
                    assert packet.topic == "retainedtest"
                    assert packet.payload == "test åäö"
                    break
    except BaseExceptionGroup as exc:
        while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
            exc = exc.exceptions[0]
        raise exc
