from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, cast

import pytest
from anyio import Event, create_task_group

from mqttproto import MQTTPublishPacket, QoS
from mqttproto.async_broker import AsyncMQTTBroker
from mqttproto.async_client import AsyncMQTTClient

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from contextlib import AbstractAsyncContextManager
    from types import TracebackType
    from typing import Any

    import anyio

    if sys.version_info < (3, 11):
        from typing_extensions import Never, Self
    else:
        from typing import Never, Self

pytestmark = [pytest.mark.anyio, pytest.mark.network]


class BrokerTest:
    __ctx: AbstractAsyncContextManager[Self]

    async def __aenter__(self) -> Self:
        self.__ctx = ctx = self._ctx()  # pylint: disable=E1101,W0201
        return await ctx.__aenter__()

    def __aexit__(
        self,
        a: type[BaseException] | None,
        b: BaseException | None,
        c: TracebackType | None,
    ) -> Any:
        return self.__ctx.__aexit__(a, b, c)

    async def run_broker(self, *, task_status: anyio.abc.TaskStatus[int]) -> Never:
        # PORT = 40000 + (os.getpid() + 21) % 10000
        """
        Runs a basic MQTT broker.

        The task status is the port we end up listening on.
        """
        broker = AsyncMQTTBroker(("127.0.0.1", 0))
        await broker.serve(task_status=task_status)
        assert False

    async def run_client(
        self, *, task_status: anyio.abc.TaskStatus[AsyncMQTTClient]
    ) -> Never:
        # PORT = 40000 + (os.getpid() + 21) % 10000
        """
        Runs a basic MQTT broker.

        The task status returns the port we're listening on.
        """
        async with AsyncMQTTClient(port=self.port) as client:
            task_status.started(client)
            await Event().wait()
            assert False

    @asynccontextmanager
    async def _ctx(self) -> AsyncIterator[Self]:
        async with create_task_group() as tg:
            self.tg = tg

            self.port = await tg.start(self.run_broker)
            yield self
            tg.cancel_scope.cancel()

    async def client(self) -> AsyncMQTTClient:
        return cast(AsyncMQTTClient, await self.tg.start(self.run_client))


@pytest.mark.parametrize(
    "qos_sub", [QoS.AT_MOST_ONCE, QoS.AT_LEAST_ONCE, QoS.EXACTLY_ONCE]
)
@pytest.mark.parametrize(
    "qos_pub", [QoS.AT_MOST_ONCE, QoS.AT_LEAST_ONCE, QoS.EXACTLY_ONCE]
)
async def test_publish_subscribe(qos_sub: QoS, qos_pub: QoS) -> None:
    qos_info = f"{qos_sub}-{qos_pub}"
    async with BrokerTest() as broker:
        client = await broker.client()
        if qos_pub > client.maximum_qos:
            return  # TODO add pytest.skip

        async with client.subscribe("test/+", maximum_qos=qos_sub) as messages:
            await client.publish(
                "test/text", "test åäö", qos=qos_pub, user_properties={"test": qos_info}
            )
            await client.publish("test/binary", b"\x00\xff\x00\x1f", qos=qos_pub)
            packets: list[MQTTPublishPacket] = []
            async for packet in messages:
                packets.append(packet)
                if len(packets) == 2:
                    break

            assert packets[0].topic == "test/text"
            assert packets[0].payload == "test åäö"
            assert packets[0].qos == min(qos_sub, qos_pub)
            assert packets[0].user_properties == {"test": qos_info}
            assert packets[1].topic == "test/binary"
            assert packets[1].payload == b"\x00\xff\x00\x1f"
            assert packets[1].qos == min(qos_sub, qos_pub)
            assert packets[1].user_properties == {}
