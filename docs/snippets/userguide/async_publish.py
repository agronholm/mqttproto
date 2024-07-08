from __future__ import annotations

import asyncio

from mqttproto.async_client import AsyncMQTTClient


async def main() -> None:
    async with AsyncMQTTClient() as client:
        await client.publish("topic", "message")


asyncio.run(main())
