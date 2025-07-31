from __future__ import annotations

import logging
import sys
from collections.abc import AsyncGenerator, Container, Sequence
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager
from ssl import SSLContext, SSLError
from types import TracebackType
from typing import TYPE_CHECKING, Any, Generic, Literal, TypeVar

import stamina
from anyio import (
    BrokenResourceError,
    ClosedResourceError,
    Event,
    Lock,
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
    PropertyType,
    PropertyValue,
    QoS,
    ReasonCode,
    RetainHandling,
    Subscription,
    Will,
)
from .client_state_machine import MQTTClientStateMachine

if sys.version_info >= (3, 11):
    from typing import Self
else:
    from typing_extensions import Self

if TYPE_CHECKING:
    from httpx_ws import AsyncWebSocketSession

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
class AsyncMQTTSubscription:
    subscriptions: Sequence[Subscription]
    send_stream: MemoryObjectSendStream[MQTTPublishPacket] = field(
        init=False, repr=False
    )
    _receive_stream: MemoryObjectReceiveStream[MQTTPublishPacket] = field(
        init=False, repr=False
    )

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
        return any(sub.matches(publish) for sub in self.subscriptions)


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
    _subscriptions: list[AsyncMQTTSubscription] = field(init=False, factory=list)
    _stream: ByteStream = field(init=False)
    _stream_lock: Lock = field(init=False, factory=Lock)
    _pending_connect: MQTTConnectOperation | None = field(init=False, default=None)
    _pending_operations: dict[int, MQTTOperation[Any]] = field(init=False, factory=dict)

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

    async def __aenter__(self) -> Self:
        async with AsyncExitStack() as exit_stack:
            task_group = await exit_stack.enter_async_context(create_task_group())
            await task_group.start(self._manage_connection)
            self._exit_stack = exit_stack.pop_all()

        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        self._closed = True

        if exc_val is None:
            self._state_machine.disconnect()
            operation = MQTTDisconnectOperation()
            await self._run_operation(operation)
            await self._stream.aclose()
        else:
            await self._stream.aclose()

        return await self._exit_stack.__aexit__(exc_type, exc_val, exc_tb)

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
                stream, ignored_exc_classes = await exit_stack.enter_async_context(cm)
                self._stream = stream

                # Start handling inbound packets
                task_group = await exit_stack.enter_async_context(create_task_group())
                task_group.start_soon(
                    self._read_inbound_packets, stream, ignored_exc_classes
                )

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

    async def _read_inbound_packets(
        self, stream: ByteReceiveStream, exception_classes: tuple[type[Exception]]
    ) -> None:
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
        except exception_classes:
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
            for sub in self._subscriptions:
                if sub.matches(packet):
                    tg.start_soon(sub.send_stream.send, packet)

    @asynccontextmanager
    async def _connect_mqtt(
        self,
    ) -> AsyncGenerator[tuple[ByteStream, tuple[type[Exception]]], None]:
        stream: ByteStream
        assert self.host_or_path
        ssl_context = self.ssl if isinstance(self.ssl, SSLContext) else None
        for attempt in stamina.retry_context(on=(OSError, SSLError)):
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
            for attempt in stamina.retry_context(on=(OSError, SSLError)):
                with attempt:
                    session = await exit_stack.enter_async_context(
                        aconnect_ws(uri, client=client, subprotocols=["mqtt"])
                    )
                    yield MQTTWebsocketStream(session), (WebSocketNetworkError,)
                    return

    async def _flush_outbound_data(self) -> None:
        async with self._stream_lock:
            if data := self._state_machine.get_outbound_data():
                await self._stream.send(data)
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
        properties: dict[PropertyType, PropertyValue] | None = None,
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
            topic, payload, qos=qos, retain=retain, properties=properties
        )
        if qos is QoS.EXACTLY_ONCE:
            assert packet_id is not None
            await self._run_operation(MQTTQoS2PublishOperation(packet_id))
        elif qos is QoS.AT_LEAST_ONCE:
            assert packet_id is not None
            await self._run_operation(MQTTQoS1PublishOperation(packet_id))
        else:
            await self._run_operation(MQTTQoS0PublishOperation())

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

        """
        if not patterns:
            raise ValueError("no subscription patterns given")

        async def unsubscribe() -> None:
            # Send an unsubscribe request if any of these patterns are not used by
            # another subscription in this client
            if unsubscribe_packet_id := self._state_machine.unsubscribe(patterns):
                unsubscribe_op = MQTTUnsubscribeOperation(unsubscribe_packet_id)
                try:
                    await self._run_operation(unsubscribe_op)
                except MQTTUnsubscribeFailed as exc:
                    logger.warning("Unsubscribe failed: %s", exc)

        async with AsyncExitStack() as exit_stack:
            subscriptions = [
                Subscription(
                    pattern=pattern,
                    max_qos=maximum_qos,
                    no_local=no_local,
                    retain_as_published=retain_as_published,
                    retain_handling=retain_handling,
                )
                for pattern in patterns
            ]

            # Set up a local subscription that will receive matching publishes as soon
            # as the broker starts sending them
            subscription = AsyncMQTTSubscription(subscriptions)
            exit_stack.enter_context(subscription)
            self._subscriptions.append(subscription)
            exit_stack.callback(self._subscriptions.remove, subscription)

            # Send a subscribe request if any of these patterns hasn't been subscribed
            # to by this client already
            if subscribe_packet_id := self._state_machine.subscribe(subscriptions):
                subscribe_op = MQTTSubscribeOperation(subscribe_packet_id)
                await self._run_operation(subscribe_op)

            # Set up an unsubscribe operation when the async context manager is exited
            exit_stack.push_async_callback(unsubscribe)
            yield subscription
