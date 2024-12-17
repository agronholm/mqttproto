from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mqttproto._types import PropertyType, ReasonCode


class MQTTException(Exception):
    """Base class for all MQTT exceptions."""


class MQTTPacketTooLarge(MQTTException):
    """
    Raised when encoding or decoding a packet, and its size exceeds the configured
    maximum packet size.
    """


class MQTTOperationFailed(MQTTException):
    def __init__(self, reason_code: ReasonCode | None = None) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class MQTTConnectFailed(MQTTOperationFailed):
    pass


class MQTTPublishFailed(MQTTOperationFailed):
    pass


class MQTTSubscribeFailed(MQTTOperationFailed):
    pass


class MQTTUnsubscribeFailed(MQTTOperationFailed):
    pass


class MQTTProtocolError(MQTTException):
    """Raised when a violation of the MQTT v5 protocol is encountered."""


class MQTTDecodeError(MQTTException):
    """Raised when something goes wrong when trying to decode an MQTT packet."""


class InsufficientData(MQTTDecodeError):
    """
    Raised when trying to decode an MQTT packet but there's not enough data to decode a
    complete packet.
    """


class MQTTUnsupportedPropertyType(MQTTDecodeError):
    """
    Raised when decoding or encoding an MQTT packet and it contains a property of a type not
    supported by that packet type.
    """

    def __init__(self, property_type: PropertyType, packet_class: type) -> None:
        super().__init__(property_type, packet_class)
        self.property_type = property_type
        self.packet_class = packet_class.__name__

    def __str__(self) -> str:
        return (
            f"unsupported property type: {self.property_type._name_} in "
            f"{self.packet_class}"
        )


class InvalidPattern(MQTTException):
    """Raised when encountering an invalid MQTT subscription pattern."""
