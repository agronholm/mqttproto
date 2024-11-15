from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import TypeVar

from attrs import define, field

from ._base_client_state_machine import BaseMQTTClientStateMachine, MQTTClientState
from ._exceptions import MQTTProtocolError
from ._types import (
    MQTTConnAckPacket,
    MQTTConnectPacket,
    MQTTDisconnectPacket,
    MQTTPacket,
    MQTTPingRequestPacket,
    MQTTPingResponsePacket,
    MQTTPublishPacket,
    MQTTSubscribeAckPacket,
    MQTTSubscribePacket,
    MQTTUnsubscribeAckPacket,
    MQTTUnsubscribePacket,
    Pattern,
    PropertyType,
    QoS,
    ReasonCode,
    Subscription,
)

TPacket = TypeVar(
    "TPacket", MQTTPublishPacket, MQTTSubscribePacket, MQTTUnsubscribePacket
)


@define(eq=False)
class MQTTBrokerStateMachine:
    """State machine for an MQTT broker."""

    client_state_machines: dict[str, MQTTBrokerClientStateMachine] = field(
        init=False, factory=dict
    )
    shared_subscriptions: dict[Pattern, dict[str, Subscription]] = field(
        init=False, factory=dict
    )
    retained_messages: dict[str, MQTTPublishPacket] = field(init=False, factory=dict)

    def add_client_session(self, session: MQTTBrokerClientStateMachine) -> None:
        if session.client_id is None:
            raise ValueError("cannot add client session without client id")

        self.client_state_machines[session.client_id] = session

    def remove_client_session(self, session: MQTTBrokerClientStateMachine) -> None:
        if session.client_id is None:
            raise ValueError("cannot add client session without client id")

        # Drop this client's subscriptions

        self.client_state_machines[session.client_id] = session
        drop = []
        for subscr in session._subscriptions:
            dest = self.shared_subscriptions.get(subscr)
            if dest is None:
                continue
            dest.pop(session.client_id, None)
            if not dest:
                drop.append(subscr)
        for subscr in drop:
            del self.shared_subscriptions[subscr]

    def subscribe_session_to(
        self, session: MQTTBrokerClientStateMachine, subscription: Subscription
    ):
        session.subscribed_to(subscription.pattern)
        dest = self.shared_subscriptions.setdefault(subscription.pattern, dict())
        dest[session.client_id] = subscription

    def unsubscribe_session_from(
        self, session: MQTTBrokerClientStateMachine, pattern: Pattern
    ) -> bool:
        """
        Unsubscribe a client from a pattern.

        Return True if the subscription existed.
        """
        session.subscribed_to(pattern)
        dest = self.shared_subscriptions.get(pattern)
        if dest is None:
            return False
        if not dest.pop(session.client_id, None):
            return False
        if not dest:
            del self.shared_subscriptions[pattern]
        return True

    def acknowledge_connect(
        self,
        client_state_machine: MQTTBrokerClientStateMachine,
        packet: MQTTConnectPacket,
        reason_code: ReasonCode,
    ) -> None:
        # Resume a previous session if the client wants to, and there was one to begin
        # with
        session_present = packet.clean_start
        if reason_code is ReasonCode.SUCCESS:
            self.client_state_machines[packet.client_id] = client_state_machine

        client_state_machine.acknowledge_connect(
            reason_code, username=packet.username, session_present=session_present
        )

    def client_disconnected(
        self, client_id: str, packet: MQTTDisconnectPacket | None
    ) -> None:
        """
        Handle a client disconnection.

        :param client_id: ID of the client that disconnected
        :param packet: the ``DISCONNECT`` packet sent by the client, or ``None`` if the
            transport stream was closed

        """
        self.client_state_machines.pop(client_id, None)
        # TODO: remove client from shared subscriptions

    def publish(
        self, source_client_id: str, packet: MQTTPublishPacket
    ) -> Collection[str]:
        """
        Publish a message from the given client to all the appropriate subscribers.

        :param source_client_id: ID of the client that published the message
        :param packet: the ``PUBLISH`` packet sent by the client
        :return: a collection of client IDs to send the publish packet to

        """
        if packet.retain:
            if packet.payload:
                self.retained_messages[packet.topic] = packet
            else:
                self.retained_messages.pop(packet.topic, None)

        recipients: set[str] = set()
        for pattern, clients in self.shared_subscriptions.items():
            if pattern.matches(packet):
                for client_id, subscr in clients.items():
                    client = self.client_state_machines.get(client_id)
                    if not subscr.no_local or source_client_id != client_id:
                        client.deliver_publish(
                            topic=packet.topic,
                            payload=packet.payload,
                            retain=packet.retain,
                            qos=min(packet.qos, subscr.max_qos),
                            user_properties=packet.user_properties,
                        )
                        recipients.add(client.client_id)

        return recipients


@define
class MQTTBrokerClientStateMachine(BaseMQTTClientStateMachine):
    """State machine for the MQTT broker's view of a client session."""

    client_id: str | None = field(init=False, default=None)
    _username: str | None = field(init=False, default=None)
    _subscriptions: set[Pattern] = field(init=False, factory=set)

    @property
    def username(self) -> str | None:
        """The username the client authenticated as."""
        return self._username

    def subscribed_to(self, pattern: Pattern):
        self._subscriptions.add(pattern)

    def unsubscribed_from(self, pattern: Pattern):
        self._subscriptions.discard(pattern)

    def _handle_packet(self, packet: MQTTPacket) -> bool:
        if super()._handle_packet(packet):
            return True

        if isinstance(packet, MQTTPingRequestPacket):
            self._in_require_state(packet, MQTTClientState.CONNECTED)
            MQTTPingResponsePacket().encode(self._out_buffer)
        elif isinstance(packet, (MQTTSubscribePacket, MQTTUnsubscribePacket)):
            self._in_require_state(packet, MQTTClientState.CONNECTED)
            if not self._add_pending_packet(packet):
                return True
        elif isinstance(packet, MQTTConnectPacket):
            self._in_require_state(packet, MQTTClientState.DISCONNECTED)
            self._state = MQTTClientState.CONNECTING
            self.client_id = packet.client_id
        elif isinstance(packet, MQTTDisconnectPacket):
            self._in_require_state(packet, MQTTClientState.CONNECTED)
            self._state = MQTTClientState.DISCONNECTED
        else:
            return False

        return True

    def deliver_publish(
        self,
        topic: str,
        payload: str | bytes,
        *,
        qos: QoS = QoS.AT_MOST_ONCE,
        retain: bool = False,
        user_properties: dict[str, str] | None = None,
    ) -> int | None:
        """
        Deliver a ``PUBLISH`` message to this client if the current state allows it.

        :param topic: topic to publish the message on
        :param payload: the actual message to publish
        :param qos:
        :param retain: ``True`` to send the message to any future subscribers of the
            topic too
        :return: the packet ID if ``qos`` was higher than 0
        """
        self._out_require_state(MQTTClientState.CONNECTED)
        packet_id = self._generate_packet_id() if qos > QoS.AT_MOST_ONCE else None
        packet = MQTTPublishPacket(
            topic=topic,
            payload=payload,
            qos=qos,
            retain=retain,
            packet_id=packet_id,
            user_properties=user_properties or {},
        )
        packet.encode(self._out_buffer)
        if packet.packet_id is not None:
            self._add_pending_packet(packet)

        return packet.packet_id

    def acknowledge_connect(
        self, reason_code: ReasonCode, username: str | None, session_present: bool
    ) -> None:
        """
        Respond to a ``CONNECT`` request by the client.

        :param reason_code: the reason code indicating either success or failure
        :param username: the username the client authenticated as
        :param session_present: ``True`` if a previously existing session was resumed,
            ``False`` if not

        """
        self._out_require_state(MQTTClientState.CONNECTING)
        if reason_code is ReasonCode.SUCCESS:
            self._state = MQTTClientState.CONNECTED
            self._username = username
        else:
            self._state = MQTTClientState.DISCONNECTED

        ack = MQTTConnAckPacket(
            reason_code=reason_code, session_present=session_present
        )
        ack.properties[PropertyType.SUBSCRIPTION_IDENTIFIER_AVAILABLE] = False
        ack.encode(self._out_buffer)

    def acknowledge_subscribe(
        self, packet_id: int, reason_codes: Sequence[ReasonCode]
    ) -> None:
        """
        Respond to a ``SUBSCRIBE`` request by the client.

        :param packet_id: the packet ID from the ``SUBSCRIBE`` packet
        :param reason_codes: the reason code indicating either success or failure for
            the corresponding subscriptions in the original request (**MUST** be in the
            same order to be matched against the correct subscriptions)

        """
        self._out_require_state(MQTTClientState.CONNECTED)
        if not (request := self._pop_pending_packet(packet_id, MQTTSubscribePacket)):
            raise MQTTProtocolError(
                f"attempted to acknowledge a {MQTTSubscribePacket.packet_type._name_} "
                f"that was either never received or has already been acknowledged"
            )

        if len(reason_codes) != len(request.subscriptions):
            raise MQTTProtocolError(
                f"mismatch in the number of reason codes in subscription "
                f"acknowledgement: the request had {len(request.subscriptions)} but "
                f"{len(reason_codes)} reason codes were given in the acknowledgement"
            )

        MQTTSubscribeAckPacket(reason_codes=reason_codes, packet_id=packet_id).encode(
            self._out_buffer
        )

    def acknowledge_unsubscribe(
        self, packet_id: int, reason_codes: Sequence[ReasonCode]
    ) -> None:
        """
        Respond to a ``UNSUBSCRIBE`` request by the client.

        :param packet_id: the packet ID from the ``SUBSCRIBE`` packet
        :param reason_codes: the reason code indicating either success or failure for
            the corresponding subscriptions in the original request (**MUST** be in the
            same order to be matched against the correct subscriptions)

        """
        self._out_require_state(MQTTClientState.CONNECTED)
        if not (request := self._pop_pending_packet(packet_id, MQTTUnsubscribePacket)):
            raise MQTTProtocolError(
                f"attempted to acknowledge a {MQTTUnsubscribePacket.packet_type._name_} "
                f"that was either never received or has already been acknowledged"
            )

        if len(reason_codes) != len(request.patterns):
            raise MQTTProtocolError(
                f"mismatch in the number of reason codes in subscription "
                f"acknowledgement: the request had {len(request.subscriptions)} but "
                f"{len(reason_codes)} reason codes were given in the acknowledgement"
            )

        MQTTUnsubscribeAckPacket(reason_codes=reason_codes, packet_id=packet_id).encode(
            self._out_buffer
        )
