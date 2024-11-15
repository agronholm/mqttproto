from __future__ import annotations

import logging
import sys
from collections.abc import AsyncGenerator, Container
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager
from ssl import SSLContext, SSLError
from types import TracebackType
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

import stamina
from anyio import (
    BrokenResourceError,
    ClosedResourceError,
    Event,
    Lock,
    WouldBlock,
    aclose_forcefully,
    connect_tcp,
    connect_unix,
    create_memory_object_stream,
    create_task_group,
)
from anyio.abc import ByteReceiveStream, ByteStream, TaskStatus
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from anyio.streams.tls import TLSStream
from attr.validators import ge, gt, in_, instance_of, le, lt, optional
from attrs import define, field

from ._exceptions import (
    MQTTConnectFailed,
    MQTTOperationFailed,
    MQTTPublishFailed,
    MQTTSubscribeFailed,
    MQTTUnsubscribeFailed,
)
from ._types import (
    MQTTConnAckPacket,
    MQTTPacket,
    MQTTPublishAckPacket,
    MQTTPublishCompletePacket,
    MQTTPublishPacket,
    MQTTSubscribeAckPacket,
    MQTTUnsubscribeAckPacket,
    Pattern,
    PropertyType,
    QoS,
    ReasonCode,
    RetainHandling,
    Subscription,
    Will,
)
from .client_state_machine import MQTTClientStateMachine

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager
    from typing import Generic, Literal

    from httpx_ws import AsyncWebSocketSession

    if sys.version_info >= (3, 11):
        from typing import Self
    else:
        from typing_extensions import Self

logger = logging.getLogger(__name__)

TPacket = TypeVar("TPacket", bound=MQTTPacket)
TAckPacket = TypeVar(
    "TAckPacket",
    MQTTConnAckPacket,
    MQTTPublishAckPacket,
    MQTTPublishCompletePacket,
    MQTTSubscribeAckPacket,
    MQTTUnsubscribeAckPacket,
    None,
)
TOperationException = TypeVar("TOperationException", bound=MQTTOperationFailed)


class MQTTWebsocketStream(ByteStream):
    def __init__(self, session: AsyncWebSocketSession):
        self._session = session

    async def send_eof(self) -> None:
        raise NotImplementedError

    async def receive(self, max_bytes: int = 65536) -> bytes:
        from httpx_ws import WebSocketInvalidTypeReceived

        try:
            return await self._session.receive_bytes()
        except WebSocketInvalidTypeReceived as exc:
            await self._session.close()
            raise BrokenResourceError from exc

    async def send(self, item: bytes) -> None:
        await self._session.send_bytes(item)

    async def aclose(self) -> None:
        await self._session.close()


@define(eq=False)
class ClientSubscription(Subscription):
    users: set[AsyncMQTTSubscription] = field(init=False, factory=set)


@define(eq=False)
class AsyncMQTTSubscription:
    subscriptions: list[ClientSubscription] = field(init=False, factory=list)
    send_stream: MemoryObjectSendStream[MQTTPublishPacket] = field(
        init=False, repr=False
    )
    _receive_stream: MemoryObjectReceiveStream[MQTTPublishPacket] = field(
        init=False, repr=False
    )
    subscription_id: int | None = field(init=False, repr=True, default=None)

    def __attrs_post_init__(self) -> None:
        self.send_stream, self._receive_stream = create_memory_object_stream[
            MQTTPublishPacket
        ](5)

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> MQTTPublishPacket:
        return await self._receive_stream.__anext__()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self._receive_stream.close()
        self.send_stream.close()
        return None

    def matches(self, publish: MQTTPublishPacket) -> bool:
        return any(
            not sub.subscription_id and sub.matches(publish)
            for sub in self.subscriptions
        )


@define(eq=False)
class MQTTOperation(Generic[TAckPacket]):
    packet_id: int | None = None
    exception_class: type[MQTTOperationFailed] | None = field(
        kw_only=True, repr=False, default=None
    )
    success_codes: Container[ReasonCode] = field(
        kw_only=True, repr=False, default={ReasonCode.SUCCESS}
    )
    response: TAckPacket = field(init=False, repr=False)
    event: Event = field(init=False, factory=Event, repr=False)
    exception: MQTTOperationFailed | None = field(init=False, default=None, repr=False)

    @property
    def requires_response(self) -> bool:
        return bool(self.success_codes)


class MQTTConnectOperation(MQTTOperation[MQTTConnAckPacket]):
    def __init__(self) -> None:
        super().__init__(exception_class=MQTTConnectFailed)


class MQTTQoS0PublishOperation(MQTTOperation[None]):
    def __init__(self) -> None:
        super().__init__(success_codes=())


class MQTTQoS1PublishOperation(MQTTOperation[MQTTPublishAckPacket]):
    def __init__(self, packet_id: int):
        super().__init__(packet_id, exception_class=MQTTPublishFailed)


class MQTTQoS2PublishOperation(MQTTOperation[MQTTPublishCompletePacket]):
    def __init__(self, packet_id: int):
        super().__init__(packet_id, exception_class=MQTTPublishFailed)


class MQTTSubscribeOperation(MQTTOperation[MQTTSubscribeAckPacket]):
    def __init__(self, packet_id: int):
        super().__init__(
            packet_id,
            exception_class=MQTTSubscribeFailed,
            success_codes={
                ReasonCode.GRANTED_QOS_0,
                ReasonCode.GRANTED_QOS_1,
                ReasonCode.GRANTED_QOS_2,
            },
        )


class MQTTUnsubscribeOperation(MQTTOperation[MQTTUnsubscribeAckPacket]):
    def __init__(self, packet_id: int):
        super().__init__(packet_id, exception_class=MQTTUnsubscribeFailed)


class MQTTDisconnectOperation(MQTTOperation[None]):
    def __init__(self) -> None:
        super().__init__(success_codes=())


@define(eq=False)
class AsyncMQTTClient:
    """
    An asynchronous MQTT client.

    Must be used as an asynchronous context manager.

    :param host_or_path: host name or UNIX socket path on the file system (defaults to
        ``localhost`` for TCP based connections; must be provided for UNIX socket
        connections)
    :param port: port number to connect to in case of the ``tcp`` transport (defaults to
        1883 or 8883 for direct MQTT depending on the ``ssl`` parameter; 80 or 443 for
        websocket connections)
    :param transport: either ``tcp`` (TCP) or ``unix`` (UNIX domain sockets)
    :param websocket_path: the URL path in the HTTP request when using websockets to
        connect to the MQTT broker (**must** be set to use websockets instead of direct
        MQTT)
    :param ssl: to use TLS (SSL), set to either ``True`` (to use the default SSL
        context), or provide your own custom SSL context here
    :param client_id: custom client ID to use when connecting to the MQTT broker (needs
        to stay constant across restarts for session resumption to work)
    :param username: user name to use for authenticating against the MQTT broker
    :param password: password to use for authenticating against the MQTT broker
    :param clean_start: if ``False``, try to resume a previous session (tied to the
        client ID)
    :param receive_maximum: number of unconfirmed QoS 1/2 messages that the client is
        willing to store at once
    :param max_packet_size: maximum packet size in bytes, or ``None`` for no limit
    :param will: message that will be published by the broker on the client's behalf if
        the client disconnects unexpectedly or fails to communicate within the keepalive
        time
    """

    host_or_path: str | None = field(default=None, validator=optional(instance_of(str)))
    port: int | None = field(default=None, validator=optional([gt(0), lt(65536)]))
    transport: Literal["tcp", "unix"] = field(
        kw_only=True, default="tcp", validator=in_({"tcp", "unix"})
    )
    websocket_path: str | None = field(
        kw_only=True, default=None, validator=optional(instance_of(str))
    )
    ssl: bool | SSLContext = field(
        kw_only=True, default=False, validator=instance_of((bool, SSLContext))
    )
    client_id: str | None = field(
        kw_only=True, validator=optional(instance_of(str)), default=None
    )
    username: str | None = field(
        kw_only=True, default=None, validator=optional(instance_of(str))
    )
    password: str | None = field(
        kw_only=True, default=None, validator=optional(instance_of(str))
    )
    clean_start: bool = field(kw_only=True, default=True, validator=instance_of(bool))
    receive_maximum: int = field(
        kw_only=True, validator=[instance_of(int), ge(0), le(65535)], default=65535
    )  # TODO: enforce this
    max_packet_size: int | None = field(
        kw_only=True, validator=optional([instance_of(int), gt(0)]), default=None
    )  # TODO: enforce this when encoding packets
    will: Will | None = field(
        kw_only=True, default=None, validator=optional(instance_of(Will))
    )

    _exit_stack: AsyncExitStack = field(init=False)
    _closed: bool = field(init=False, default=False)
    _state_machine: MQTTClientStateMachine = field(init=False)
    _subscribe_lock: Lock = field(init=False, factory=Lock)
    _subscriptions: dict[Pattern, ClientSubscription] = field(init=False, factory=dict)
    _subscription_ids: dict[int, ClientSubscription] = field(init=False, factory=dict)
    _subscription_no_id: dict[Pattern, ClientSubscription] = field(
        init=False, factory=dict
    )
    _last_subscr_id: int = field(init=False, default=0)
    _stream: ByteStream = field(init=False)
    _stream_lock: Lock = field(init=False, factory=Lock)
    _pending_connect: MQTTConnectOperation | None = field(init=False, default=None)
    _pending_operations: dict[int, MQTTOperation[Any]] = field(init=False, factory=dict)
    _ignored_exc_classes: tuple[type[Exception]] = field(init=False)
    __ctx: AbstractAsyncContextManager[Self] = field(init=False)

    def __attrs_post_init__(self) -> None:
        if not self.host_or_path:
            if self.transport == "unix":
                raise ValueError(
                    "host_or_path must be a filesystem path when using the 'unix' "
                    "transport"
                )
            else:
                self.host_or_path = "localhost"

        if not self.port and self.transport != "unix":
            if self.websocket_path:
                self.port = 443 if self.ssl else 80
            else:
                self.port = 8883 if self.ssl else 1883

        self._state_machine = MQTTClientStateMachine(client_id=self.client_id)

    @property
    def may_retain(self) -> bool:
        return self._state_machine.may_retain

    @property
    def may_subscription_id(self) -> bool:
        return self._state_machine.may_subscription_id

    async def __aenter__(self) -> Self:
        ctx: AbstractAsyncContextManager[Self]
        self.__ctx = ctx = self._ctx()
        return await ctx.__aenter__()

    def __aexit__(
        self,
        a: type[BaseException] | None,
        b: BaseException | None,
        c: TracebackType | None,
    ) -> Any:
        return self.__ctx.__aexit__(a, b, c)

    @asynccontextmanager
    async def _ctx(self) -> AsyncGenerator[Self]:
        async with create_task_group() as task_group:
            await task_group.start(self._manage_connection)
            try:
                yield self
            except BaseException:
                await self._stream.aclose()
                raise
            else:
                self._state_machine.disconnect()
                operation = MQTTDisconnectOperation()
                await self._run_operation(operation)

                await self._stream.aclose()
            finally:
                task_group.cancel_scope.cancel()

    async def _manage_connection(
        self,
        *,
        task_status: TaskStatus[None],
    ) -> None:
        task_status_sent = False
        while not self._closed:
            # Establish the transport stream
            if self.websocket_path:
                cm = self._connect_ws()
            else:
                cm = self._connect_mqtt()

            async with AsyncExitStack() as exit_stack:
                (
                    stream,
                    self._ignored_exc_classes,
                ) = await exit_stack.enter_async_context(cm)
                self._stream = stream

                # Start handling inbound packets
                task_group = await exit_stack.enter_async_context(create_task_group())
                task_group.start_soon(self._read_inbound_packets, stream)

                # Perform the MQTT handshake (send conn + receive connack)
                await self._do_handshake()

                # Signal that the client is ready
                if not task_status_sent:
                    task_status.started()
                    task_status_sent = True

    async def _do_handshake(self) -> None:
        self._state_machine.connect(
            username=self.username,
            password=self.password,
            will=self.will,
        )
        operation = MQTTConnectOperation()
        await self._run_operation(operation)

        # If the broker assigned us a client id, save that
        if client_id := operation.response.properties.get(
            PropertyType.ASSIGNED_CLIENT_IDENTIFIER
        ):
            assert isinstance(client_id, str)
            self.client_id = client_id

    async def _read_inbound_packets(self, stream: ByteReceiveStream) -> None:
        # Receives packets from the transport stream and forwards them to interested
        # listeners
        try:
            async for chunk in stream:
                logger.debug("Received bytes from transport stream: %r", chunk)
                received_packets = self._state_machine.feed_bytes(chunk)
                await self._flush_outbound_data()

                # Send any received publications to subscriptions that want them
                for packet in received_packets:
                    await self._handle_packet(packet)
        except self._ignored_exc_classes:
            pass

    async def _handle_packet(self, packet: MQTTPacket) -> None:
        if isinstance(packet, MQTTPublishPacket):
            await self._deliver_publish(packet)
        elif isinstance(
            packet,
            (
                MQTTPublishAckPacket,
                MQTTPublishCompletePacket,
                MQTTSubscribeAckPacket,
                MQTTUnsubscribeAckPacket,
            ),
        ):
            operation = self._pending_operations.pop(packet.packet_id)
            operation.response = packet
            operation.event.set()
        elif isinstance(packet, MQTTConnAckPacket):
            assert self._pending_connect is not None
            self._pending_connect.response = packet
            self._pending_connect.event.set()
            self._pending_connect = None

    async def _deliver_publish(self, packet: MQTTPublishPacket) -> None:
        async with create_task_group() as tg:

            def send(subscr: ClientSubscription) -> None:
                for client in subscr.users:
                    try:
                        client.send_stream.send_nowait(packet)
                    except WouldBlock:
                        tg.start_soon(client.send_stream.send, packet)

            if subscr_ids := packet.properties.get(
                PropertyType.SUBSCRIPTION_IDENTIFIER
            ):
                for subscr_id in cast("list[int]", subscr_ids):
                    if sub := self._subscription_ids.get(subscr_id):
                        send(sub)
            else:
                for sub in self._subscription_no_id.values():
                    if sub.matches(packet):
                        send(sub)

    @asynccontextmanager
    async def _connect_mqtt(
        self,
    ) -> AsyncGenerator[tuple[ByteStream, tuple[type[Exception]]], None]:
        stream: ByteStream
        assert self.host_or_path
        ssl_context = self.ssl if isinstance(self.ssl, SSLContext) else None

        async for attempt in stamina.retry_context(on=(OSError, SSLError)):
            with attempt:
                if self.transport == "unix":
                    stream = await connect_unix(self.host_or_path)
                else:
                    assert self.port
                    stream = await connect_tcp(self.host_or_path, self.port)

                if self.ssl:
                    try:
                        stream = await TLSStream.wrap(
                            stream,
                            hostname=self.host_or_path,
                            ssl_context=ssl_context,
                        )
                    except BaseException:
                        await aclose_forcefully(stream)
                        raise

                yield stream, (ClosedResourceError,)
                return

    @asynccontextmanager
    async def _connect_ws(
        self,
    ) -> AsyncGenerator[tuple[ByteStream, tuple[type[Exception]]], None]:
        from httpx import AsyncClient
        from httpx_ws import WebSocketNetworkError, aconnect_ws

        scheme = "wss" if self.ssl else "ws"
        uri = f"{scheme}://{self.host_or_path}:{self.port}{self.websocket_path}"

        # MQTT-6.0.0-3
        async with AsyncExitStack() as exit_stack:
            client = await exit_stack.enter_async_context(AsyncClient(verify=self.ssl))
            async for attempt in stamina.retry_context(on=(OSError, SSLError)):
                with attempt:
                    session = await exit_stack.enter_async_context(
                        aconnect_ws(uri, client=client, subprotocols=["mqtt"])
                    )
                    yield MQTTWebsocketStream(session), (WebSocketNetworkError,)
                    return

    async def _flush_outbound_data(self) -> None:
        async with self._stream_lock:
            if data := self._state_machine.get_outbound_data():
                try:
                    await self._stream.send(data)
                except self._ignored_exc_classes as exc:
                    logger.debug("Skip bytes to transport stream: %r: %r", data, exc)
                else:
                    logger.debug("Sent bytes to transport stream: %r", data)

    async def _run_operation(self, operation: MQTTOperation[Any]) -> None:
        with ExitStack() as exit_stack:
            if operation.packet_id is not None:
                self._pending_operations[operation.packet_id] = operation
                exit_stack.callback(
                    self._pending_operations.pop, operation.packet_id, None
                )
            elif isinstance(operation, MQTTConnectOperation):
                self._pending_connect = operation
                exit_stack.callback(setattr, self, "_pending_connect", None)

            await self._flush_outbound_data()

            if not operation.requires_response:
                return

            await operation.event.wait()

        if operation.exception:
            raise operation.exception

    @property
    def maximum_qos(self) -> QoS:
        """
        Returns the maximum QoS level that the broker supports.
        """
        return self._state_machine.maximum_qos

    async def publish(
        self,
        topic: str,
        payload: bytes | str,
        *,
        qos: QoS = QoS.AT_MOST_ONCE,
        retain: bool = False,
        user_properties: dict[str, str] | None = None,
    ) -> None:
        """
        Publish a message to the given topic.

        :param topic: the topic to publish the message to
        :param payload: the message to publish
        :param qos: the QoS of the message
        :param retain: if ``True``, the message will be always be sent to clients that
            subscribe to the given topic even after the message was already published
            before the subscription happened

        """
        packet_id = self._state_machine.publish(
            topic, payload, qos=qos, retain=retain, user_properties=user_properties
        )
        if qos is QoS.EXACTLY_ONCE:
            assert packet_id is not None
            await self._run_operation(MQTTQoS2PublishOperation(packet_id))
        elif qos is QoS.AT_LEAST_ONCE:
            assert packet_id is not None
            await self._run_operation(MQTTQoS1PublishOperation(packet_id))
        else:
            await self._run_operation(MQTTQoS0PublishOperation())

    def _new_subscr_id(self) -> int:
        if not self.may_subscription_id:
            return 0

        sid = self._last_subscr_id
        while True:
            sid += 1
            # If there are sufficiently large and/or many holes in the list
            # of assigned subscription IDs, restart from the beginning:
            # larger IDs take up more space in the message.
            if (
                sid in (128, 16384)
                and len(self._subscription_ids) < self._last_subscr_id / 5
            ):
                sid = 1

            if sid in self._subscription_ids:
                continue

            self._last_subscr_id = sid
            return sid

    @asynccontextmanager
    async def subscribe(
        self,
        *patterns: str,
        maximum_qos: QoS = QoS.EXACTLY_ONCE,
        no_local: bool = False,
        retain_as_published: bool = True,
        retain_handling: RetainHandling = RetainHandling.SEND_RETAINED,
    ) -> AsyncGenerator[AsyncMQTTSubscription, None]:
        """
        Subscribe to the given topics or topic patterns.

        :param patterns: either exact topic names, or patterns containing wildcards
            (``+`` or ``#``)
        :param maximum_qos: maximum QoS to allow (messages matching the given patterns
            but with higher QoS will be downgraded to that QoS)
        :param no_local: if ``True``, messages published by this client will not be sent
            back to it via this subscription
        :param retain_as_published: if ``False``, the broker will clear the ``retained``
            flag of all published messages sent to the client
        :param retain_handling:
            * If set to ``SEND_RETAINED`` (the default), the broker will send any
              retained messages matching this subscription to this client
            * If set to ``SEND_RETAINED_IF_NOT_SUBSCRIBED``, then the broker will only
              send retained messages if this subscription did not already exist
            * If set to ``NO_RETAINED``, then retained messages will not be sent to this
              client
        :return: an async context manager that will yield messages matching the
            subscribed topics/patterns

        Subscribing to the same topic pattern multiple times is supported,
        with these restrictions:

            * the `retain_as_published` and `no_local` flags must match.

            * subsequent subscriptions can't use `RetainHandling.SEND_RETAINED`.

            * When a second subscription raises the subscription's maximum QoS,
              it's not lowered when the second subscription ends.
        """
        if not patterns:
            raise ValueError("no subscription patterns given")

        async def unsubscribe() -> None:
            # Send an unsubscribe request if any of my subscriptions are unused
            async with self._subscribe_lock:
                patterns: list[Pattern] = []
                for subscr in subscription.subscriptions:
                    subscr.users.discard(subscription)
                    if not subscr.users:
                        patterns.append(subscr.pattern)
                        del self._subscriptions[subscr.pattern]
                        if subscr.subscription_id:
                            del self._subscription_ids[subscr.subscription_id]
                        else:
                            del self._subscription_no_id[pattern]

                if patterns:
                    if unsubscribe_packet_id := self._state_machine.unsubscribe(
                        patterns
                    ):
                        unsubscribe_op = MQTTUnsubscribeOperation(unsubscribe_packet_id)
                        try:
                            await self._run_operation(unsubscribe_op)
                        except MQTTUnsubscribeFailed as exc:
                            logger.warning("Unsubscribe failed: %s", exc)

        async with AsyncExitStack() as exit_stack:
            subscription = AsyncMQTTSubscription()
            exit_stack.enter_context(subscription)
            exit_stack.push_async_callback(unsubscribe)

            to_subscribe: list[ClientSubscription] = []

            async with self._subscribe_lock:
                for pattern_str in patterns:
                    pattern = Pattern(pattern_str)
                    if subscr := self._subscriptions.get(pattern):
                        if subscr.no_local != no_local:
                            raise ValueError(
                                f"Conflicting 'no_local' option for {pattern}"
                            )
                        if subscr.retain_as_published != retain_as_published:
                            raise ValueError(
                                f"Conflicting 'retain_as_published' option for {pattern}"
                            )

                        if retain_handling == RetainHandling.SEND_RETAINED:
                            raise ValueError(
                                f"Already subscribed: cannot get retained messages for {pattern}"
                            )

                        if subscr.max_qos >= maximum_qos:
                            # nothing to do
                            continue
                    else:
                        subscr = ClientSubscription(
                            pattern=pattern,
                            max_qos=maximum_qos,
                            no_local=no_local,
                            retain_as_published=retain_as_published,
                            retain_handling=retain_handling,
                            subscription_id=self._new_subscr_id(),
                        )
                        self._subscriptions[pattern] = subscr
                        if subscr.subscription_id:
                            self._subscription_ids[subscr.subscription_id] = subscr
                        else:
                            self._subscription_no_id[pattern] = subscr

                    # link the subscription
                    subscription.subscriptions.append(subscr)
                    subscr.users.add(subscription)

                    if subscr.subscription_id:
                        # every topic has (in fact, needs) its own ID.
                        subscribe_packet_id = self._state_machine.subscribe(
                            [subscr], max_qos=maximum_qos
                        )
                        subscribe_op = MQTTSubscribeOperation(subscribe_packet_id)
                        await self._run_operation(subscribe_op)
                        subscr.max_qos = max(maximum_qos, subscr.max_qos)
                    else:
                        to_subscribe.append(subscr)

                if to_subscribe:
                    # no subscription IDs, so do it all at once
                    subscribe_packet_id = self._state_machine.subscribe(
                        to_subscribe, max_qos=maximum_qos
                    )
                    subscribe_op = MQTTSubscribeOperation(subscribe_packet_id)
                    await self._run_operation(subscribe_op)
                    for subscr in to_subscribe:
                        subscr.max_qos = max(maximum_qos, subscr.max_qos)

            yield subscription
