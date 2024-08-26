from __future__ import annotations

import asyncio

from anyio import create_task_group
from anyio.abc import TaskStatus

from mqttproto.async_client import AsyncMQTTClient


async def subscriber1(
    client: AsyncMQTTClient, *, task_status: TaskStatus[None]
) -> None:
    async with client.subscribe("more/+/topics") as sub:
        task_status.started()
        async for message in sub:
            print(
                f"subscriber1: received a message from {message.topic}: "
                f"{message.payload!r}"
            )


async def subscriber2(
    client: AsyncMQTTClient, *, task_status: TaskStatus[None]
) -> None:
    async with client.subscribe("other/#") as sub:
        task_status.started()
        async for message in sub:
            print(
                f"subscriber2: received a message from {message.topic}: "
                f"{message.payload!r}"
            )


async def main() -> None:
    async with AsyncMQTTClient() as client, create_task_group() as task_group:
        await task_group.start(subscriber1, client)
        await task_group.start(subscriber2, client)


asyncio.run(main())
