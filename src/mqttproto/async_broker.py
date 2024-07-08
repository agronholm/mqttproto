from __future__ import annotations

import logging
from abc import ABCMeta, abstractmethod
from collections.abc import Sequence
from os import PathLike
from ssl import SSLContext
from typing import Any

import anyio
from anyio import Lock, create_task_group, create_tcp_listener, create_unix_listener
from anyio.abc import (
    ByteStream,
    Listener,
    SocketAttribute,
)
from attrs import define, field

from mqttproto import (
    MQTTDisconnectPacket,
    MQTTPublishPacket,
)
from mqttproto._base_client_state_machine import MQTTClientState
from mqttproto._types import (
    MQTTConnectPacket,
    MQTTPacket,
    ReasonCode,
)
from mqttproto.broker_state_machine import (
    MQTTBrokerClientStateMachine,
    MQTTBrokerStateMachine,
)

logger = logging.getLogger(__name__)


@define(eq=False)
class AsyncMQTTClientSession:
    stream: ByteStream
    state_machine: MQTTBrokerClientStateMachine = field(
        init=False, factory=MQTTBrokerClientStateMachine
    )
    lock: Lock = field(init=False, factory=Lock)

    async def flush_outbound_data(self) -> None:
        async with self.lock:
            if data := self.state_machine.get_outbound_data():
                await self.stream.send(data)


class MQTTAuthenticator(metaclass=ABCMeta):
    @abstractmethod
    async def authenticate(
        self,
        client_id: str,
        username: str | None,
        password: str | None,
        stream: ByteStream,
    ) -> ReasonCode | None:
        """
        Determine whether the given client should be authenticated.

        :param client_id: the client ID
        :param username: the username presented in the ``CONNECT`` packet
        :param password: the password presented in the ``CONNECT`` packet
        :param stream: the client's transport stream; could be used, for example, for:

            * IP whitelist/blacklist checking
            * strong UNIX authentication
            * TLS client certificate checking
        :return: a reason code to either allow or deny the login, or ``None`` to
            continue with the next authenticator
        """


class MQTTAuthorizer(metaclass=ABCMeta):
    @abstractmethod
    async def authorize_publish(
        self,
        topic: str,
        client_id: str,
        username: str | None,
        stream: ByteStream,
    ) -> ReasonCode | None:
        """
        Determine whether the given client should be authenticated.

        :param topic: the topic in the ``PUBLISH`` packet
        :param client_id: the client ID
        :param username: the username presented in the ``CONNECT`` packet
        :param stream: the client's transport stream
        :return: a reason code to either allow or deny the publish, or ``None`` to
            continue with the next authorizer
        """

    @abstractmethod
    async def authorize_subscribe(
        self,
        pattern: str,
        client_id: str,
        username: str | None,
        stream: ByteStream,
    ) -> ReasonCode | None:
        """
        Determine whether the given client should be allowed to subscribe to the given
        topic pattern.

        :param pattern: the topic filter
        :param client_id: the client ID
        :param username: the username presented in the ``CONNECT`` packet
        :param stream: the client's transport stream
        :return: a reason code to either allow or deny the subscription, or ``None`` to
            continue with the next authorizer
        """


@define
class AsyncMQTTBroker:
    """
    A simple MQTT broker implementation.

    :param bind_address: either a tuple of (address, port) or a path to a local UNIX
        socket to bind to
    :param ssl_context: an SSL context which, if given, will be used to do the TLS
        handshake with incoming connections
    :param authenticators: a sequence of authenticators that will be called, starting
        from the first one, to authenticate a login attempt
    :param authorizers: a sequence of authorizers that will be called, starting
        from the first one, to authenticate ``PUBLISH`` and ``SUBSCRIBE`` requests
    """

    bind_address: tuple[str, int] | str | bytes | PathLike[str] | PathLike[bytes] = (
        "127.0.0.1",
        1883,
    )
    ssl_context: SSLContext | None = None  # TODO: use this to serve TLS
    authenticators: Sequence[MQTTAuthenticator] = field(factory=list)
    authorizers: Sequence[MQTTAuthorizer] = field(factory=list)
    _state_machine: MQTTBrokerStateMachine = field(init=False)
    _client_sessions: dict[str, AsyncMQTTClientSession] = field(
        init=False, factory=dict
    )

    async def serve_client(self, stream: ByteStream) -> None:
        """
        Called to handle a connected client.

        :param stream: the byte stream for the client.

        """
        async with stream:
            session = AsyncMQTTClientSession(stream=stream)
            async for chunk in stream:
                for packet in session.state_machine.feed_bytes(chunk):
                    await self.handle_packet(
                        packet, session.state_machine, session.stream
                    )

                if session.state_machine.state is MQTTClientState.DISCONNECTED:
                    return

                await session.flush_outbound_data()

            if session.state_machine.state is MQTTClientState.DISCONNECTED:
                self._state_machine.client_disconnected(
                    session.state_machine.client_id, None
                )

    async def handle_packet(
        self,
        packet: MQTTPacket,
        client_state_machine: MQTTBrokerClientStateMachine,
        stream: ByteStream,
    ) -> None:
        """
        Called by :meth:`serve_client` to handle an MQTT packet received from the
        client.

        :param packet: the received packet
        :param client_state_machine: the client session state machine
        :param stream: the client's transport stream

        """
        if isinstance(packet, MQTTPublishPacket):
            reason_code = await self._authorize_publish(
                packet, client_state_machine, stream
            )
            if packet.packet_id is not None:
                client_state_machine.acknowledge_publish(packet.packet_id, reason_code)

            if reason_code is ReasonCode.SUCCESS:
                async with create_task_group() as tg:
                    for client_session in [
                        self._client_sessions[client_id]
                        for client_id in self._state_machine.publish(
                            client_state_machine.client_id, packet
                        )
                    ]:
                        tg.start_soon(client_session.flush_outbound_data)
        elif isinstance(packet, MQTTConnectPacket):
            reason_code = await self._authorize_connect(packet, stream)
            self._state_machine.acknowledge_connect(
                client_state_machine, packet, reason_code
            )
            self._state_machine.add_client_session(client_state_machine)
        elif isinstance(packet, MQTTDisconnectPacket):
            if client_state_machine.state is MQTTClientState.CONNECTED:
                self._state_machine.client_disconnected(
                    client_state_machine.client_id, packet
                )

    def add_client_session(self, session: AsyncMQTTClientSession) -> None:
        self._state_machine.add_client_session(session.state_machine)
        self._client_sessions[session.state_machine.client_id] = session

    def remove_client_session(self, session: AsyncMQTTClientSession) -> None:
        self._state_machine.remove_client_session(session.state_machine)
        del self._client_sessions[session.state_machine.client_id]

    async def _authorize_connect(
        self, packet: MQTTConnectPacket, stream: ByteStream
    ) -> ReasonCode:
        for authenticator in self.authenticators:
            reason_code = await authenticator.authenticate(
                packet.client_id, packet.username, packet.password, stream
            )
            if reason_code is not None:
                return reason_code

        return ReasonCode.SUCCESS

    async def _authorize_subscribe(
        self,
        pattern: str,
        client_state_machine: MQTTBrokerClientStateMachine,
        stream: ByteStream,
    ) -> ReasonCode:
        for authorizer in self.authorizers:
            reason_code = await authorizer.authorize_subscribe(
                pattern,
                client_state_machine.client_id,
                client_state_machine.username,
                stream,
            )
            if reason_code is not None:
                return reason_code

        return ReasonCode.SUCCESS

    async def _authorize_publish(
        self,
        packet: MQTTPublishPacket,
        client_state_machine: MQTTBrokerClientStateMachine,
        stream: ByteStream,
    ) -> ReasonCode:
        for authorizer in self.authorizers:
            reason_code = await authorizer.authorize_publish(
                packet.topic,
                client_state_machine.client_id,
                client_state_machine.username,
                stream,
            )
            if reason_code is not None:
                return reason_code

        return ReasonCode.SUCCESS

    async def serve(self) -> None:
        listener: Listener[Any]
        if isinstance(self.bind_address, (str, bytes, PathLike)):
            listener = await create_unix_listener(self.bind_address)
        else:
            host, port = self.bind_address  # TODO: this is problematic for IPv6
            listener = await create_tcp_listener(local_host=host, local_port=port)

        logger.info(
            "Broker listening on %s", listener.extra(SocketAttribute.local_address)
        )
        await listener.serve(self.serve_client)


if __name__ == "__main__":
    anyio.run(AsyncMQTTBroker().serve)
