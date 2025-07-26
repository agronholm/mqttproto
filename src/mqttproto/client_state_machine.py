from __future__ import annotations

from collections.abc import Sequence
from typing import cast
from uuid import uuid4

from attr.validators import instance_of, min_len
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
    Will,
)


@define(eq=False, init=False)
class MQTTClientStateMachine(BaseMQTTClientStateMachine):
    """State machine for a client's session with an MQTT broker."""

    client_id: str = field(validator=[instance_of(str), min_len(1)])
    _ping_pending: bool = field(init=False, default=False)
    _may_retain: bool = field(init=False, default=True)
    _may_subscription_id: bool = field(init=False, default=True)
    _maximum_qos: QoS = field(init=False, default=QoS.EXACTLY_ONCE)

    def __init__(self, client_id: str | None = None):
        self.__attrs_init__(client_id=client_id or f"mqttproto-{uuid4().hex}")
        self._auto_ack_publishes = True

    @property
    def may_retain(self) -> bool:
        """Does the server support RETAINed messages?"""
        return self._may_retain

    @property
    def may_subscription_id(self) -> bool:
        """Does the server support subscription IDs?"""
        return self._may_subscription_id

    def reset(self, session_present: bool) -> None:
        self._ping_pending = False
        if session_present:
            self._pending_packets = {
                packet_id: packet
                for packet_id, packet in self._pending_packets.items()
                if isinstance(packet, MQTTPublishPacket)
            }
        else:
            self._next_packet_id = 1

    def _handle_packet(self, packet: MQTTPacket) -> bool:
        if super()._handle_packet(packet):
            return True

        if isinstance(packet, MQTTConnAckPacket):
            self._in_require_state(packet, MQTTClientState.CONNECTING)
            if packet.reason_code is ReasonCode.SUCCESS:
                self._state = MQTTClientState.CONNECTED
                self._auth_method = cast(
                    str, packet.properties.get(PropertyType.AUTHENTICATION_METHOD)
                )
                self._may_retain = cast(
                    bool, packet.properties.get(PropertyType.RETAIN_AVAILABLE, True)
                )
                self._may_subscription_id = cast(
                    bool,
                    packet.properties.get(
                        PropertyType.SUBSCRIPTION_IDENTIFIER_AVAILABLE, True
                    ),
                )
                self._maximum_qos = cast(
                    QoS,
                    packet.properties.get(PropertyType.MAXIMUM_QOS, QoS.EXACTLY_ONCE),
                )

                self.reset(session_present=packet.session_present)

                # Resend any pending publishes (and set the duplicate flag)
                for publish in self._pending_packets.values():
                    assert isinstance(publish, MQTTPublishPacket)
                    publish.duplicate = True

                    publish.encode(self._out_buffer)
            else:
                self._state = MQTTClientState.DISCONNECTED
        elif isinstance(packet, MQTTPingResponsePacket):
            self._in_require_state(packet, MQTTClientState.CONNECTED)
            self._ping_pending = False
        elif isinstance(packet, MQTTSubscribeAckPacket):
            self._in_require_state(packet, MQTTClientState.CONNECTED)
            self._pop_pending_packet(packet.packet_id, MQTTSubscribePacket)
        elif isinstance(packet, MQTTUnsubscribeAckPacket):
            self._in_require_state(packet, MQTTClientState.CONNECTED)
            self._pop_pending_packet(packet.packet_id, MQTTUnsubscribePacket)
        elif isinstance(packet, MQTTDisconnectPacket):
            self._in_require_state(
                packet, MQTTClientState.CONNECTING, MQTTClientState.CONNECTED
            )
            self._state = MQTTClientState.DISCONNECTED
        else:
            return False

        return True

    def connect(
        self,
        *,
        username: str | None = None,
        password: str | None = None,
        will: Will | None = None,
        clean_start: bool = True,
        keep_alive: int = 0,
    ) -> None:
        self._out_require_state(MQTTClientState.DISCONNECTED)
        packet = MQTTConnectPacket(
            client_id=self.client_id,
            will=will,
            username=username,
            password=password,
            clean_start=clean_start,
            keep_alive=keep_alive,
        )
        packet.encode(self._out_buffer)
        self._state = MQTTClientState.CONNECTING

    def disconnect(self, reason_code: ReasonCode = ReasonCode.SUCCESS) -> None:
        self._out_require_state(MQTTClientState.CONNECTED)
        packet = MQTTDisconnectPacket(reason_code=reason_code)
        packet.encode(self._out_buffer)
        self._state = MQTTClientState.DISCONNECTED

    def ping(self) -> None:
        self._out_require_state(MQTTClientState.CONNECTED)
        packet = MQTTPingRequestPacket()
        packet.encode(self._out_buffer)
        self._ping_pending = True

    def publish(
        self,
        topic: str,
        payload: str | bytes,
        *,
        qos: QoS = QoS.AT_MOST_ONCE,
        retain: bool = False,
        user_properties: dict[str, str] | None = None,
    ) -> int | None:
        """
        Send a ``PUBLISH`` request.

        :param topic: topic to publish the message on
        :param payload: the actual message to publish
        :param qos:
        :param retain: ``True`` to send the message to any future subscribers of the
            topic too
        :return: the packet ID if ``qos`` was higher than 0

        A QoS that's not supported by the server is silently downgraded.
        If Retain is not supported, the message is sent as-is because
        the server is free to accept it anyway.
        """
        self._out_require_state(MQTTClientState.CONNECTED)
        packet_id = self._generate_packet_id() if qos > QoS.AT_MOST_ONCE else None
        packet = MQTTPublishPacket(
            topic=topic,
            payload=payload,
            qos=qos,
            retain=retain,
            packet_id=packet_id,
            user_properties={} if user_properties is None else user_properties,
        )
        packet.encode(self._out_buffer)
        if packet_id is not None:
            self._add_pending_packet(packet)

        return packet.packet_id

    @property
    def maximum_qos(self) -> QoS:
        """
        Returns the maximum QoS level that the broker supports.
        """
        return self._maximum_qos

    def subscribe(
        self, subscriptions: Sequence[Subscription], max_qos: QoS | None = None
    ) -> int:
        """
        Subscribe to one or more topic patterns.

        Send a ``SUBSCRIBE`` request, containing one of more subscriptions.

        All included subscriptions need to use the same subscription ID.

        :param subscriptions: a sequence of subscriptions
        :param max_qos: the maximum QoS to request. Leave as `None` to use the
            subscription's value.
        :return: packet ID of the ``SUBSCRIBE`` request.

        When changing a subscription's QoS, pass the new max_qos value.
        Update the subscription record after the operation was successful.
        """
        subscr_id: int | None = None

        # TODO remember the subscription for reconnect if not
        self._out_require_state(MQTTClientState.CONNECTED)

        for sub in subscriptions:
            # If all subscriptions use a common ID, collect and use that.
            # Otherwise don't.
            if subscr_id is None:
                subscr_id = sub.subscription_id
            elif subscr_id != sub.subscription_id:
                raise ValueError("Inconsistent subscription IDs")

        packet = MQTTSubscribePacket(
            subscriptions=subscriptions,
            packet_id=self._generate_packet_id(),
            max_qos=max_qos,
        )
        if subscr_id and self.may_subscription_id:
            packet.properties[PropertyType.SUBSCRIPTION_IDENTIFIER] = subscr_id
        packet.encode(self._out_buffer)
        self._add_pending_packet(packet)
        return packet.packet_id

    def unsubscribe(self, patterns: Sequence[Pattern]) -> int | None:
        """
        Unsubscribe from one or more topic patterns.

        Send an ``UNSUBSCRIBE`` request, containing one of more subscriptions.

        :param patterns: topic patterns to unsubscribe from
        :return: packet ID of the ``UNSUBSCRIBE`` request
        """
        try:
            self._out_require_state(MQTTClientState.CONNECTED)
        except MQTTProtocolError:
            return None
            # TODO remember the unsubscription for reconnect

        packet = MQTTUnsubscribePacket(
            patterns=patterns, packet_id=self._generate_packet_id()
        )
        packet.encode(self._out_buffer)
        self._add_pending_packet(packet)
        return packet.packet_id
