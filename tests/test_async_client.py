from __future__ import annotations

import sys

import pytest

from mqttproto import MQTTPublishPacket, QoS
from mqttproto.async_client import AsyncMQTTClient

if sys.version_info < (3, 11):
    from exceptiongroup import BaseExceptionGroup

pytestmark = [pytest.mark.anyio, pytest.mark.network]


@pytest.mark.parametrize(
    "qos_sub", [QoS.AT_MOST_ONCE, QoS.AT_LEAST_ONCE, QoS.EXACTLY_ONCE]
)
@pytest.mark.parametrize(
    "qos_pub", [QoS.AT_MOST_ONCE, QoS.AT_LEAST_ONCE, QoS.EXACTLY_ONCE]
)
async def test_publish_subscribe(qos_sub: QoS, qos_pub: QoS) -> None:
    async with AsyncMQTTClient() as client:
        if qos_pub > client.maximum_qos:
            return  # TODO add pytest.skip

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
            assert packets[0].qos == min(qos_sub, qos_pub)
            assert packets[1].topic == "test/binary"
            assert packets[1].payload == b"\x00\xff\x00\x1f"
            assert packets[1].qos == min(qos_sub, qos_pub)


async def test_publish_overlap() -> None:
    # Same as above but there's another overlapping suscription
    # so we need to skip duplicates IF the server doesn't filter them
    async with AsyncMQTTClient() as client:
        async with client.subscribe("test/+") as messages, client.subscribe("#"):
            await client.publish("test/text", "test åäö")
            await client.publish("test/binary", b"\x00\xff\x00\x1f")
            packets: list[MQTTPublishPacket] = []
            async for packet in messages:
                if (
                    not client.may_subscription_id
                    and packets
                    and packets[0].topic == "test/text"
                ):
                    assert not client.may_subscription_id
                    # with subscription IDs this won't happen
                    continue

                packets.append(packet)
                if len(packets) == 2:
                    break

            assert packets[0].topic == "test/text"
            assert packets[0].payload == "test åäö"
            assert packets[1].topic == "test/binary"
            assert packets[1].payload == b"\x00\xff\x00\x1f"


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
