from __future__ import annotations

import sys
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from ssl import SSLContext
from types import TracebackType
from typing import Any, Literal

from anyio.from_thread import BlockingPortal, BlockingPortalProvider
from attrs import define

from ._types import (
    MQTTPublishPacket,
    PropertyType,
    PropertyValue,
    QoS,
    RetainHandling,
    Will,
)
from .async_client import AsyncMQTTClient, AsyncMQTTSubscription

if sys.version_info >= (3, 11):
    from typing import Self
else:
    from typing_extensions import Self

portal_provider = BlockingPortalProvider()


@define(eq=False, repr=False, slots=True)
class MQTTSubscription:
    async_subscription: AsyncMQTTSubscription
    portal: BlockingPortal

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> MQTTPublishPacket:
        try:
            return self.portal.call(self.async_subscription.__anext__)
        except StopAsyncIteration:
            raise StopIteration from None


class MQTTClient:
    _exit_stack: ExitStack
    _portal: BlockingPortal

    def __init__(
        self,
        host_or_path: str | None = None,
        port: int | None = None,
        *,
        transport: Literal["tcp", "unix"] = "tcp",
        websocket_path: str | None = None,
        ssl: bool | SSLContext = False,
        client_id: str | None = None,
        username: str | None = None,
        password: str | None = None,
        clean_start: bool = True,
        receive_maximum: int = 65535,
        max_packet_size: int | None = None,
        will: Will | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {}
        if client_id is not None:
            kwargs["client_id"] = client_id

        self._async_client = AsyncMQTTClient(
            host_or_path,
            port,
            transport=transport,
            websocket_path=websocket_path,
            ssl=ssl,
            username=username,
            password=password,
            clean_start=clean_start,
            receive_maximum=receive_maximum,
            max_packet_size=max_packet_size,
            will=will,
            **kwargs,
        )

    @property
    def may_retain(self) -> bool:
        return self._async_client.may_retain

    def __enter__(self) -> Self:
        with ExitStack() as exit_stack:
            self._portal = exit_stack.enter_context(portal_provider)
            exit_stack.enter_context(
                self._portal.wrap_async_context_manager(self._async_client)
            )
            self._exit_stack = exit_stack.pop_all()

        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        return self._exit_stack.__exit__(exc_type, exc_val, exc_tb)

    def publish(
        self,
        topic: str,
        payload: bytes | str,
        *,
        qos: QoS = QoS.AT_MOST_ONCE,
        retain: bool = False,
        properties: dict[PropertyType, PropertyValue] | None = None,
    ) -> None:
        return self._portal.call(
            lambda: self._async_client.publish(
                topic, payload, qos=qos, retain=retain, properties=properties
            )
        )

    @contextmanager
    def subscribe(
        self,
        *patterns: str,
        maximum_qos: QoS = QoS.EXACTLY_ONCE,
        no_local: bool = False,
        retain_as_published: bool = True,
        retain_handling: RetainHandling = RetainHandling.SEND_RETAINED,
    ) -> Generator[MQTTSubscription, None, None]:
        async_cm = self._async_client.subscribe(
            *patterns,
            maximum_qos=maximum_qos,
            no_local=no_local,
            retain_as_published=retain_as_published,
            retain_handling=retain_handling,
        )
        with self._portal.wrap_async_context_manager(async_cm) as async_subscription:
            yield MQTTSubscription(async_subscription, self._portal)


# Copy the docstrings from the async variant
for attrname in dir(AsyncMQTTClient):
    if attrname.startswith("_"):
        continue

    value = getattr(AsyncMQTTClient, attrname)
    if callable(value):
        sync_method = getattr(MQTTClient, attrname, None)
        if sync_method and not sync_method.__doc__:
            sync_method.__doc__ = value.__doc__
