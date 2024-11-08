from __future__ import annotations

import pytest

from mqttproto import MQTTPublishPacket, QoS
from mqttproto.async_client import AsyncMQTTClient

pytestmark = [pytest.mark.anyio, pytest.mark.network]


@pytest.mark.parametrize(
    "qos_sub", [QoS.AT_MOST_ONCE, QoS.AT_LEAST_ONCE, QoS.EXACTLY_ONCE]
)
@pytest.mark.parametrize(
    "qos_pub", [QoS.AT_MOST_ONCE, QoS.AT_LEAST_ONCE, QoS.EXACTLY_ONCE]
)
async def test_publish_subscribe(qos_sub: QoS, qos_pub: QoS) -> None:
    async with AsyncMQTTClient() as client:
        async with client.subscribe("test/+", maximum_qos=qos_sub) as messages:
            await client.publish("test/text", "test åäö", qos=qos_pub)
            await client.publish("test/binary", b"\x00\xff\x00\x1f", qos=qos_pub)
            packets: list[MQTTPublishPacket] = []
            async for packet in messages:
                packets.append(packet)
                if len(packets) == 2:
                    break

            assert packets[0].topic == "test/text"
            assert packets[0].payload == "test åäö"
            assert packets[1].topic == "test/binary"
            assert packets[1].payload == b"\x00\xff\x00\x1f"


async def test_retained_message() -> None:
    async with AsyncMQTTClient() as client:
        await client.publish("retainedtest", "test åäö", retain=True)
        async with client.subscribe("retainedtest") as messages:
            async for packet in messages:
                assert packet.topic == "retainedtest"
                assert packet.payload == "test åäö"
                break
