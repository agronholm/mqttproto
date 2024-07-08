from __future__ import annotations

import asyncio

from mqttproto.async_client import AsyncMQTTClient


async def main() -> None:
    async with AsyncMQTTClient() as client:
        async with client.subscribe("topic") as sub:
            async for message in sub:
                print(f"Received a message: {message.payload!r}")


asyncio.run(main())
