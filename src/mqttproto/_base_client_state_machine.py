from __future__ import annotations

import logging
from collections.abc import Sequence
from enum import Enum, auto
from typing import TypeVar

from attr.validators import instance_of
from attrs import define, field

from ._exceptions import InsufficientData, MQTTProtocolError
from ._types import (
    MQTTAuthPacket,
    MQTTDisconnectPacket,
    MQTTPacket,
    MQTTPublishAckPacket,
    MQTTPublishCompletePacket,
    MQTTPublishPacket,
    MQTTPublishReceivePacket,
    MQTTPublishReleasePacket,
    MQTTSubscribeAckPacket,
    MQTTSubscribePacket,
    MQTTUnsubscribeAckPacket,
    MQTTUnsubscribePacket,
    PublishAckState,
    QoS,
    ReasonCode,
    decode_packet,
)

logger = logging.getLogger("mqttproto")

TPacket = TypeVar(
    "TPacket", MQTTPublishPacket, MQTTSubscribePacket, MQTTUnsubscribePacket
)


class MQTTClientState(Enum):
    DISCONNECTED = auto()
    CONNECTING = auto()
    CONNECTED = auto()


@define(eq=False)
class BaseMQTTClientStateMachine:
    """Base class for MQTT client session state machines."""

    client_id: str = field(init=False, validator=instance_of(str))
    _out_buffer: bytearray = field(init=False, factory=bytearray)
    _in_buffer: bytearray = field(init=False, factory=bytearray)
    _state: MQTTClientState = field(init=False, default=MQTTClientState.DISCONNECTED)
    _auth_method: str | None = field(init=False, default=None)
    _pending_packets: dict[
        int, MQTTPublishPacket | MQTTSubscribePacket | MQTTUnsubscribePacket
    ] = field(init=False, factory=dict)
    _received_packets: list[MQTTPacket] = field(init=False, factory=list)
    _next_packet_id: int = field(init=False, default=1)
    _auto_ack_publishes: bool = field(init=False, default=False)

    @property
    def state(self) -> MQTTClientState:
        """The current state of the client."""
        return self._state

    @property
    def auth_method(self) -> str | None:
        """The authentication method chosen by the client."""
        return self._auth_method

    def _handle_packet(self, packet: MQTTPacket) -> bool:
        if isinstance(packet, MQTTPublishPacket):
            self._in_require_state(packet, MQTTClientState.CONNECTED)
            if not self._add_pending_packet(packet):
                return True

            if packet.packet_id and self._auto_ack_publishes:
                self.acknowledge_publish(packet.packet_id, ReasonCode.SUCCESS)
        elif isinstance(packet, MQTTPublishAckPacket):
            self._in_require_state(packet, MQTTClientState.CONNECTED)
            publish = self._pop_pending_packet(packet.packet_id, MQTTPublishPacket)
            if publish and publish.qos is not QoS.AT_LEAST_ONCE:
                raise MQTTProtocolError(
                    f"received unexpected {packet.packet_type._name_} for a QoS "
                    f"{int(publish.qos)} publish packet"
                )
        elif isinstance(packet, MQTTPublishReceivePacket):
            self._in_require_state(packet, MQTTClientState.CONNECTED)
            publish = self._pop_pending_packet(
                packet.packet_id, MQTTPublishPacket, remove=False
            )
            if publish:
                if publish.qos is not QoS.EXACTLY_ONCE:
                    raise MQTTProtocolError(
                        f"received unexpected {publish.packet_type._name_} for a QoS "
                        f"{int(publish.qos)} publish packet"
                    )

                if publish.ack_state is not PublishAckState.UNACKNOWLEDGED:
                    raise MQTTProtocolError(
                        f"received {packet.packet_type._name_} for a publish packet in "
                        f"the {publish.ack_state._name_} state"
                    )

                publish.ack_state = PublishAckState.ACKNOWLEDGED
                reason_code = ReasonCode.SUCCESS
            else:
                reason_code = ReasonCode.PACKET_IDENTIFIER_NOT_FOUND

            if reason_code is not ReasonCode.SUCCESS or self._auto_ack_publishes:
                self.release_qos2_publish(packet.packet_id, reason_code)
        elif isinstance(packet, MQTTPublishReleasePacket):
            self._in_require_state(packet, MQTTClientState.CONNECTED)
            publish = self._pop_pending_packet(
                packet.packet_id, MQTTPublishPacket, remove=False
            )
            if publish:
                if publish.qos is not QoS.EXACTLY_ONCE:
                    raise MQTTProtocolError(
                        f"received unexpected {publish.packet_type._name_} for a QoS "
                        f"{int(publish.qos)} publish packet"
                    )

                if publish.ack_state is not PublishAckState.ACKNOWLEDGED:
                    raise MQTTProtocolError(
                        f"received {packet.packet_type._name_} for a publish packet in "
                        f"the {publish.ack_state._name_} state"
                    )

                publish.ack_state = PublishAckState.RELEASED
                reason_code = ReasonCode.SUCCESS
            else:
                reason_code = ReasonCode.PACKET_IDENTIFIER_NOT_FOUND

            self.complete_qos2_publish(packet.packet_id, reason_code)
        elif isinstance(packet, MQTTPublishCompletePacket):
            self._in_require_state(packet, MQTTClientState.CONNECTED)
            publish = self._pop_pending_packet(packet.packet_id, MQTTPublishPacket)
            if publish:
                if publish.qos is not QoS.EXACTLY_ONCE:
                    raise MQTTProtocolError(
                        f"received unexpected {publish.packet_type._name_} for a QoS "
                        f"{int(publish.qos)} publish packet"
                    )

                if publish.ack_state is not PublishAckState.RELEASED:
                    raise MQTTProtocolError(
                        f"received {packet.packet_type._name_} for a publish packet in "
                        f"the {publish.ack_state._name_} state"
                    )
            else:
                raise MQTTProtocolError(
                    f"received {packet.packet_type._name_} for a publish packet that "
                    f"was not previously acknowledged"
                )
        elif isinstance(packet, MQTTDisconnectPacket):
            self._in_require_state(packet, MQTTClientState.CONNECTED)
            self._state = MQTTClientState.DISCONNECTED
        elif isinstance(packet, MQTTAuthPacket):
            self._in_require_state(
                packet, MQTTClientState.CONNECTED, MQTTClientState.CONNECTING
            )
        else:
            return False

        return True

    def _generate_packet_id(self) -> int:
        packet_id = self._next_packet_id
        self._next_packet_id += 1
        if self._next_packet_id == 65536:
            self._next_packet_id = 1

        return packet_id

    def _in_require_state(self, packet: MQTTPacket, *states: MQTTClientState) -> None:
        if self._state not in states:
            allowed_states = ", ".join(state.name for state in states)
            raise MQTTProtocolError(
                f"received unexpected {type(packet).__name__}; the client session is "
                f"currently in the {self._state.name} state but needs to be in one of "
                f"the following states to send this: {allowed_states}"
            )

    def _out_require_state(self, *states: MQTTClientState) -> None:
        if self._state not in states:
            allowed_states = ", ".join(state.name for state in states)
            raise MQTTProtocolError(
                f"cannot perform this operation in the {self._state.name} state (must "
                f"be in one of the following states: {allowed_states})"
            )

    def _add_pending_packet(
        self,
        packet: MQTTPublishPacket | MQTTSubscribePacket | MQTTUnsubscribePacket,
    ) -> bool:
        if packet.packet_id is None:
            return True

        response: (
            MQTTPublishAckPacket
            | MQTTPublishReceivePacket
            | MQTTSubscribeAckPacket
            | MQTTUnsubscribeAckPacket
        )
        if self._pending_packets.setdefault(packet.packet_id, packet) is not packet:
            if isinstance(packet, MQTTPublishPacket):
                if packet.qos is QoS.AT_LEAST_ONCE:
                    response = MQTTPublishAckPacket(
                        reason_code=ReasonCode.PACKET_IDENTIFIER_IN_USE,
                        packet_id=packet.packet_id,
                    )
                else:
                    response = MQTTPublishReceivePacket(
                        reason_code=ReasonCode.PACKET_IDENTIFIER_IN_USE,
                        packet_id=packet.packet_id,
                    )
            elif isinstance(packet, MQTTSubscribePacket):
                reason_codes = [
                    ReasonCode.PACKET_IDENTIFIER_IN_USE for _ in packet.subscriptions
                ]
                response = MQTTSubscribeAckPacket(
                    reason_codes=reason_codes, packet_id=packet.packet_id
                )
            else:
                reason_codes = [
                    ReasonCode.PACKET_IDENTIFIER_IN_USE for _ in packet.patterns
                ]
                response = MQTTSubscribeAckPacket(
                    reason_codes=reason_codes, packet_id=packet.packet_id
                )

            response.encode(self._out_buffer)
            return False

        return True

    def _pop_pending_packet(
        self, packet_id: int, packet_class: type[TPacket], *, remove: bool = True
    ) -> TPacket | None:
        try:
            if remove:
                request = self._pending_packets.pop(packet_id)
            else:
                request = self._pending_packets[packet_id]
        except KeyError:
            return None

        if isinstance(request, packet_class):
            return request

        raise MQTTProtocolError(
            f"attempted to send an acknowledgement for {packet_class.__name__} that "
            f"either never arrived or was already acknowledged"
        )

    def get_outbound_data(self) -> bytes:
        """
        Retrieve any bytes to be sent to the peer.

        This method is not idempotent, as the returned bytes are removed from the
        outbound data buffer.

        """
        buffer = self._out_buffer
        self._out_buffer = bytearray()
        return buffer

    def feed_bytes(self, data: bytes) -> Sequence[MQTTPacket]:
        """
        Input bytes received from the transport stream to the state machine.

        The state machine parses and validates any packets found in the stream and
        alters its state accordingly. If the last packet is incomplete, it will remain
        in the inbound data buffer until a later call which completes the packet.

        :param data: bytes received from the transport stream
        :return: a sequence of complete packets parsed from the bytes

        """
        self._in_buffer.extend(data)
        view = memoryview(self._in_buffer)
        received_packets: list[MQTTPacket] = []
        while view:
            try:
                view, packet = decode_packet(view)
            except InsufficientData:
                break

            if not self._handle_packet(packet):
                raise MQTTProtocolError(
                    f"received unexpected {packet.packet_type._name_} packet"
                )

            logger.debug("Received packet from broker: %r", packet)
            received_packets.append(packet)

        cutoff_offset = len(self._in_buffer) - len(view)
        if cutoff_offset:
            self._in_buffer = self._in_buffer[cutoff_offset:]

        return received_packets

    def acknowledge_publish(self, packet_id: int, reason_code: ReasonCode) -> None:
        self._out_require_state(MQTTClientState.CONNECTED)
        if not (request := self._pop_pending_packet(packet_id, MQTTPublishPacket)):
            raise MQTTProtocolError(
                f"attempted to acknowledge a {MQTTPublishPacket.packet_type._name_} "
                f"that was either never received or has already been acknowledged"
            )

        if request.qos is QoS.AT_LEAST_ONCE:
            MQTTPublishAckPacket(reason_code=reason_code, packet_id=packet_id).encode(
                self._out_buffer
            )
        else:
            MQTTPublishReceivePacket(
                reason_code=reason_code, packet_id=packet_id
            ).encode(self._out_buffer)
            self._add_pending_packet(request)

        request.ack_state = PublishAckState.ACKNOWLEDGED

    def release_qos2_publish(self, packet_id: int, reason_code: ReasonCode) -> None:
        self._out_require_state(MQTTClientState.CONNECTED)
        if reason_code is not ReasonCode.PACKET_IDENTIFIER_NOT_FOUND:
            if not (
                request := self._pop_pending_packet(
                    packet_id, MQTTPublishPacket, remove=False
                )
            ):
                raise MQTTProtocolError(
                    f"attempted to acknowledge a {MQTTPublishPacket.packet_type._name_} "
                    f"that was either never received or has already been completed"
                )

            if request.qos is not QoS.EXACTLY_ONCE:
                raise MQTTProtocolError("attempted to release a QoS 1 publish")

            if request.ack_state is not PublishAckState.ACKNOWLEDGED:
                raise MQTTProtocolError(
                    f"attempted to release a QoS 2 publish in the "
                    f"{request.ack_state._name_} state"
                )

            request.ack_state = PublishAckState.RELEASED

        MQTTPublishReleasePacket(reason_code=reason_code, packet_id=packet_id).encode(
            self._out_buffer
        )

    def complete_qos2_publish(self, packet_id: int, reason_code: ReasonCode) -> None:
        self._out_require_state(MQTTClientState.CONNECTED)
        if reason_code is not ReasonCode.PACKET_IDENTIFIER_NOT_FOUND:
            if not (request := self._pop_pending_packet(packet_id, MQTTPublishPacket)):
                raise MQTTProtocolError(
                    f"attempted to acknowledge a {MQTTPublishPacket.packet_type._name_} "
                    f"that was either never received or has already been completed"
                )

            if request.qos is not QoS.EXACTLY_ONCE:
                raise MQTTProtocolError(
                    "attempted to send a QoS 2 completion for a QoS 1 publish"
                )

            if request.ack_state is not PublishAckState.RELEASED:
                raise MQTTProtocolError(
                    f"attempted to complete a QoS 2 publish in the "
                    f"{request.ack_state._name_} state"
                )

        MQTTPublishCompletePacket(reason_code=reason_code, packet_id=packet_id).encode(
            self._out_buffer
        )
