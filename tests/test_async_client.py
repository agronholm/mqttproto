from __future__ import annotations

import sys

import pytest

from mqttproto import MQTTPublishPacket, QoS
from mqttproto.async_client import AsyncMQTTClient

pytestmark = [pytest.mark.anyio, pytest.mark.network]


@pytest.mark.parametrize("qos", [QoS.AT_MOST_ONCE, QoS.AT_LEAST_ONCE, QoS.EXACTLY_ONCE])
async def test_publish_subscribe(qos: QoS) -> None:
    async with AsyncMQTTClient() as client:
        async with client.subscribe("test/+") as messages:
            await client.publish("test/text", "test åäö", qos=qos)
            await client.publish("test/binary", b"\x00\xff\x00\x1f", qos=qos)
            packets: list[MQTTPublishPacket] = []
            async for packet in messages:
                packets.append(packet)
                if len(packets) == 2:
                    break

            assert packets[0].topic == "test/text"
            assert packets[0].payload == "test åäö"
            assert packets[1].topic == "test/binary"
            assert packets[1].payload == b"\x00\xff\x00\x1f"


if sys.version_info < (3, 11):

    class BaseExceptionGroup(BaseException):
        exceptions: list[BaseExceptionGroup] = []


async def test_retained_message() -> None:
    try:
        async with AsyncMQTTClient() as client:
            if not client.may_retain:
                pytest.skip("Retain not available")
            await client.publish("retainedtest", "test åäö", retain=True)
            async with client.subscribe("retainedtest") as messages:
                async for packet in messages:
                    assert packet.topic == "retainedtest"
                    assert packet.payload == "test åäö"
                    break
    except BaseExceptionGroup as exc:
        while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
            exc = exc.exceptions[0]
        raise exc
