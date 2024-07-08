from __future__ import annotations

import asyncio

from mqttproto.async_client import AsyncMQTTClient


async def main() -> None:
    async with AsyncMQTTClient(host_or_path="localhost", ssl=True) as client:
        ...


asyncio.run(main())
