from __future__ import annotations

import sys
from abc import ABCMeta, abstractmethod
from collections.abc import Callable, Sequence
from enum import Enum, IntEnum, auto
from functools import partial
from itertools import zip_longest
from typing import Any, ClassVar, cast

from attrs import define, field
from attrs.validators import deep_iterable, in_, instance_of

from ._exceptions import (
    InsufficientData,
    InvalidPattern,
    MQTTDecodeError,
    MQTTPacketTooLarge,
    MQTTProtocolError,
    MQTTUnsupportedPropertyType,
)

if sys.version_info >= (3, 11):
    from typing import Self
else:
    from typing_extensions import Self

if sys.version_info >= (3, 10):
    from typing import TypeAlias
else:
    from typing_extensions import TypeAlias

PropertyValue: TypeAlias = "str | bytes | int | tuple[str, str]"

VARIABLE_HEADER_START = b"\x00\x04MQTT\x05"

packet_types: dict[int, type[MQTTPacket]] = {}


def encode_fixed_integer(value: int, buffer: bytearray, size: int) -> None:
    buffer.extend(value.to_bytes(size, "big"))


def decode_fixed_integer(data: memoryview, size: int) -> tuple[memoryview, int]:
    if len(data) < size:
        raise InsufficientData

    return data[size:], int.from_bytes(data[:size], "big")


def encode_variable_integer(value: int, buffer: bytearray) -> None:
    assert value >= 0, "Cannot pack negative values"
    while True:
        new_byte = value % 128
        value //= 128
        if value > 0:
            new_byte |= 128

        buffer.append(new_byte)
        if value == 0:
            return


def decode_variable_integer(data: memoryview) -> tuple[memoryview, int]:
    multiplier = 1
    value = 0
    for i, val in enumerate(data, 1):
        value += (val & 127) * multiplier
        multiplier *= 128
        if not val & 128:
            return data[i:], value

    raise InsufficientData


def encode_binary(value: bytes, buffer: bytearray) -> None:
    encode_fixed_integer(len(value), buffer, 2)
    buffer.extend(value)


def decode_binary(data: memoryview) -> tuple[memoryview, bytes]:
    data, length = decode_fixed_integer(data, 2)
    if len(data) < length:
        raise InsufficientData

    return data[length:], bytes(data[:length])


def encode_utf8(value: str, buffer: bytearray) -> None:
    data = value.encode("utf-8")
    encode_fixed_integer(len(data), buffer, 2)
    buffer.extend(data)


def decode_utf8(data: memoryview) -> tuple[memoryview, str]:
    data, length = decode_fixed_integer(data, 2)
    if len(data) < length:
        raise InsufficientData

    try:
        return data[length:], str(data[:length], "utf-8")
    except UnicodeDecodeError as exc:
        raise MQTTDecodeError(f"error decoding utf-8 string: {exc}") from None


def encode_utf8_pair(value: tuple[str, str], buffer: bytearray) -> None:
    encode_utf8(value[0], buffer)
    encode_utf8(value[1], buffer)


def decode_utf8_pair(data: memoryview) -> tuple[memoryview, tuple[str, str]]:
    data, string1 = decode_utf8(data)
    data, string2 = decode_utf8(data)
    return data, (string1, string2)


class ControlPacketType(IntEnum):
    CONNECT = 1
    CONNACK = 2
    PUBLISH = 3
    PUBACK = 4
    PUBREC = 5
    PUBREL = 6
    PUBCOMP = 7
    SUBSCRIBE = 8
    SUBACK = 9
    UNSUBSCRIBE = 10
    UNSUBACK = 11
    PINGREQ = 12
    PINGRESP = 13
    DISCONNECT = 14
    AUTH = 15


class ReasonCode(IntEnum):
    SUCCESS = 0x00
    NORMAL_DISCONNECTION = 0x00
    GRANTED_QOS_0 = 0x00
    GRANTED_QOS_1 = 0x01
    GRANTED_QOS_2 = 0x02
    DISCONNECT_WITH_WILL_MESSAGE = 0x04
    NO_MATCHING_SUBSCRIBERS = 0x10
    NO_SUBSCRIPTION_EXISTED = 0x11
    CONTINUE_AUTHENTICATION = 0x18
    REAUTHENTICATE = 0x19
    UNSPECIFIED_ERROR = 0x80
    MALFORMED_PACKET = 0x81
    PROTOCOL_ERROR = 0x82
    IMPLEMENTATION_SPECIFIC_ERROR = 0x83
    UNSUPPORTED_PROTOCOL_VERSION = 0x84
    CLIENT_IDENTIFIER_NOT_VALID = 0x85
    BAD_USERNAME_OR_PASSWORD = 0x86
    NOT_AUTHORIZED = 0x87
    SERVER_UNAVAILABLE = 0x88
    SERVER_BUSY = 0x89
    BANNED = 0x8A
    SERVER_SHUTTING_DOWN = 0x8B
    BAD_AUTHENTICATION_METHOD = 0x8C
    KEEP_ALIVE_TIMEOUT = 0x8D
    SESSION_TAKEN_OVER = 0x8E
    TOPIC_FILTER_INVALID = 0x8F
    TOPIC_NAME_INVALID = 0x90
    PACKET_IDENTIFIER_IN_USE = 0x91
    PACKET_IDENTIFIER_NOT_FOUND = 0x92
    RECEIVE_MAXIMUM_EXCEEDED = 0x93
    TOPIC_ALIAS_INVALID = 0x94
    PACKET_TOO_LARGE = 0x95
    MESSAGE_RATE_TOO_HIGH = 0x96
    QUOTA_EXCEEDED = 0x97
    ADMINISTRATIVE_ACTION = 0x98
    PAYLOAD_FORMAT_INVALID = 0x99
    RETAIN_NOT_SUPPORTED = 0x9A
    QOS_NOT_SUPPORTED = 0x9B
    USE_ANOTHER_SERVER = 0x9C
    SERVER_MOVED = 0x9D
    SHARED_SUBSCRIPTIONS_NOT_SUPPORTED = 0x9E
    CONNECTION_RATE_EXCEEDED = 0x9F
    MAXIMUM_CONNECT_TIME = 0xA0
    SUBSCRIPTION_IDENTIFIERS_NOT_SUPPORTED = 0xA1
    WILDCARD_SUBSCRIPTIONS_NOT_SUPPORTED = 0xA2

    @classmethod
    def get(cls, value: int) -> Self:
        for member in cls.__members__.values():
            if member.value == value:
                return member

        raise MQTTDecodeError(f"unknown reason code: 0x{value:02X}")


class RetainHandling(IntEnum):
    SEND_RETAINED = 0
    SEND_RETAINED_IF_NOT_SUBSCRIBED = 1
    NO_RETAINED = 2

    @classmethod
    def get(cls, value: int) -> Self:
        for member in cls.__members__.values():
            if member.value == value:
                return member

        raise MQTTDecodeError(f"unknown retain handling: 0x{value:02X}")


class QoS(IntEnum):
    AT_MOST_ONCE = 0
    AT_LEAST_ONCE = 1
    EXACTLY_ONCE = 2

    @classmethod
    def get(cls, value: int) -> Self:
        for member in cls.__members__.values():
            if member.value == value:
                return member

        raise MQTTDecodeError(f"unknown QoS value: 0x{value:02X}")


class PublishAckState(Enum):
    UNACKNOWLEDGED = auto()
    ACKNOWLEDGED = auto()
    RELEASED = auto()


class PropertyType(IntEnum):
    PAYLOAD_FORMAT_INDICATOR = (
        0x01,
        partial(encode_fixed_integer, size=1),
        partial(decode_fixed_integer, size=1),
    )
    MESSAGE_EXPIRY_INTERVAL = (
        0x02,
        partial(encode_fixed_integer, size=4),
        partial(decode_fixed_integer, size=4),
    )
    CONTENT_TYPE = 0x03, encode_utf8, decode_utf8
    RESPONSE_TOPIC = 0x08, encode_utf8, decode_utf8
    CORRELATION_DATA = 0x09, encode_binary, decode_binary
    # TODO: this can appear multiple times
    SUBSCRIPTION_IDENTIFIER = 0x0B, encode_variable_integer, decode_variable_integer
    SESSION_EXPIRY_INTERVAL = (
        0x11,
        partial(encode_fixed_integer, size=4),
        partial(decode_fixed_integer, size=4),
    )
    ASSIGNED_CLIENT_IDENTIFIER = 0x12, encode_utf8, decode_utf8
    SERVER_KEEP_ALIVE = (
        0x13,
        partial(encode_fixed_integer, size=2),
        partial(decode_fixed_integer, size=2),
    )
    AUTHENTICATION_METHOD = 0x15, encode_utf8, decode_utf8
    AUTHENTICATION_DATA = 0x16, encode_binary, decode_binary
    REQUEST_PROBLEM_INFORMATION = (
        0x17,
        partial(encode_fixed_integer, size=1),
        partial(decode_fixed_integer, size=1),
    )
    WILL_DELAY_INTERVAL = (
        0x18,
        partial(encode_fixed_integer, size=4),
        partial(decode_fixed_integer, size=4),
    )
    REQUEST_RESPONSE_INFORMATION = (
        0x19,
        partial(encode_fixed_integer, size=1),
        partial(decode_fixed_integer, size=1),
    )
    RESPONSE_INFORMATION = 0x1A, encode_utf8, decode_utf8
    SERVER_REFERENCE = 0x1C, encode_utf8, decode_utf8
    REASON_STRING = 0x1F, encode_utf8, decode_utf8
    RECEIVE_MAXIMUM = (
        0x21,
        partial(encode_fixed_integer, size=2),
        partial(decode_fixed_integer, size=2),
    )
    TOPIC_ALIAS_MAXIMUM = (
        0x22,
        partial(encode_fixed_integer, size=2),
        partial(decode_fixed_integer, size=2),
    )
    TOPIC_ALIAS = (
        0x23,
        partial(encode_fixed_integer, size=2),
        partial(decode_fixed_integer, size=2),
    )
    MAXIMUM_QOS = (
        0x24,
        partial(encode_fixed_integer, size=1),
        partial(decode_fixed_integer, size=1),
    )
    RETAIN_AVAILABLE = (
        0x25,
        partial(encode_fixed_integer, size=1),
        partial(decode_fixed_integer, size=1),
    )
    USER_PROPERTY = 0x26, encode_utf8_pair, decode_utf8_pair
    MAXIMUM_PACKET_SIZE = (
        0x27,
        partial(encode_fixed_integer, size=4),
        partial(decode_fixed_integer, size=4),
    )
    WILDCARD_SUBSCRIPTION_AVAILABLE = (
        0x28,
        partial(encode_fixed_integer, size=1),
        partial(decode_fixed_integer, size=1),
    )
    SUBSCRIPTION_IDENTIFIER_AVAILABLE = (
        0x29,
        partial(encode_fixed_integer, size=1),
        partial(decode_fixed_integer, size=1),
    )
    SHARED_SUBSCRIPTION_AVAILABLE = (
        0x2A,
        partial(encode_fixed_integer, size=1),
        partial(decode_fixed_integer, size=1),
    )

    encoder: Callable[[PropertyValue, bytearray], None]
    decoder: Callable[[memoryview], tuple[memoryview, PropertyValue]]

    def __new__(
        cls,
        identifier: int,
        encoder: Callable[[PropertyValue, bytearray], None],
        decoder: Callable[[memoryview], tuple[memoryview, PropertyValue]],
    ) -> PropertyType:
        instance = int.__new__(cls, identifier)
        instance._value_ = identifier
        instance.encoder = encoder
        instance.decoder = decoder
        return instance

    @classmethod
    def get(cls, value: int) -> Self:
        for member in cls.__members__.values():
            if member.value == value:
                return member

        raise MQTTDecodeError(f"unknown property type: 0x{value:02X}")


@define(kw_only=True)
class PropertiesMixin:
    allowed_property_types: ClassVar[frozenset[PropertyType]] = frozenset()
    properties: dict[PropertyType, PropertyValue] = field(repr=False, factory=dict)
    user_properties: dict[str, str] = field(repr=False, factory=dict)

    def encode_properties(self, buffer: bytearray) -> None:
        internal_buffer = bytearray()
        for identifier, value in self.properties.items():
            if identifier not in self.allowed_property_types:
                raise MQTTUnsupportedPropertyType(identifier, self.__class__)

            encode_variable_integer(identifier, internal_buffer)
            identifier.encoder(value, internal_buffer)

        for key, value in self.user_properties.items():
            encode_variable_integer(PropertyType.USER_PROPERTY, internal_buffer)
            PropertyType.USER_PROPERTY.encoder((key, value), internal_buffer)

        encode_variable_integer(len(internal_buffer), buffer)
        buffer.extend(internal_buffer)

    @classmethod
    def decode_properties(
        cls, data: memoryview
    ) -> tuple[memoryview, dict[PropertyType, PropertyValue], dict[str, str]]:
        data, length = decode_variable_integer(data)
        if len(data) < length:
            raise InsufficientData

        data, view = data[length:], data[:length]
        properties: dict[PropertyType, PropertyValue] = {}
        user_properties: dict[str, str] = {}
        while view:
            view, property_num = decode_variable_integer(view)
            property_type = PropertyType.get(property_num)
            if property_type not in cls.allowed_property_types:
                raise MQTTUnsupportedPropertyType(property_type, cls)

            view, value = property_type.decoder(view)
            if property_type is PropertyType.USER_PROPERTY:
                key, value = cast("tuple[str, str]", value)
                user_properties[key] = value
            else:
                if property_type in properties:
                    raise MQTTDecodeError(
                        f"duplicate property {property_type} when decoding "
                        f"{cls.__name__}"
                    )

                properties[property_type] = value

        return data, properties, user_properties


class ReasonCodeMixin:
    allowed_reason_codes: ClassVar[frozenset[ReasonCode]]

    @classmethod
    def decode_reason_code(cls, data: memoryview) -> tuple[memoryview, ReasonCode]:
        data, reason_code_num = decode_fixed_integer(data, 1)
        reason_code = ReasonCode.get(reason_code_num)
        if reason_code not in cls.allowed_reason_codes:
            raise MQTTDecodeError(
                f"reason code 0x{reason_code:02X} is not allowed for {cls.__name__}"
            )

        return data, reason_code


@define(kw_only=True)
class Will(PropertiesMixin):
    allowed_property_types = frozenset(
        [
            PropertyType.PAYLOAD_FORMAT_INDICATOR,
            PropertyType.MESSAGE_EXPIRY_INTERVAL,
            PropertyType.CONTENT_TYPE,
            PropertyType.RESPONSE_TOPIC,
            PropertyType.CORRELATION_DATA,
            PropertyType.WILL_DELAY_INTERVAL,
            PropertyType.USER_PROPERTY,
        ]
    )

    topic: str
    payload: str | bytes
    retain: bool = False
    qos: QoS = QoS.AT_MOST_ONCE

    def __attrs_post_init__(self) -> None:
        if isinstance(self.payload, str):
            self.properties[PropertyType.PAYLOAD_FORMAT_INDICATOR] = True


@define(eq=False)
class Subscription:
    QOS_MASK = 3
    NO_LOCAL_FLAG = 4
    RETAIN_AS_PUBLISHED_FLAG = 8
    RETAIN_HANDLING_MASK = 48

    pattern: str
    max_qos: QoS = field(kw_only=True, default=QoS.EXACTLY_ONCE)
    no_local: bool = field(kw_only=True, default=False)
    retain_as_published: bool = field(kw_only=True, default=True)
    retain_handling: RetainHandling = field(
        kw_only=True, default=RetainHandling.SEND_RETAINED
    )
    group_id: str | None = field(init=False, default=None)
    _parts: tuple[str, ...] = field(init=False, repr=False, eq=False)

    def __attrs_post_init__(self) -> None:
        self._parts = tuple(self.pattern.split("/"))
        for i, part in enumerate(self._parts):
            if "+" in part and len(part) != 1:
                # MQTT-4.7.1-2
                raise InvalidPattern(
                    "single-level wildcard ('+') must occupy an entire level of the "
                    "topic filter"
                )
            elif "#" in part:
                if len(part) != 1:
                    # MQTT-4.7.1-1
                    raise InvalidPattern(
                        "multi-level wildcard ('#') must occupy an entire level of the "
                        "topic filter"
                    )
                elif i != len(self._parts) - 1:
                    # MQTT-4.7.1-1
                    raise InvalidPattern(
                        "multi-level wildcard ('#') must be the last character in the "
                        "topic filter"
                    )

        # Save the group ID for a shared subscription
        if len(self._parts) > 2 and self._parts[0] == "$share":
            self.group_id = self._parts[1]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Subscription):
            return self.pattern == other.pattern

        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.pattern)

    @classmethod
    def decode(cls, data: memoryview) -> tuple[memoryview, Subscription]:
        data, pattern = decode_utf8(data)
        data, options = decode_fixed_integer(data, 1)
        qos = QoS.get(options & cls.QOS_MASK)
        no_local = bool(options & cls.NO_LOCAL_FLAG)
        retain_as_published = bool(options & cls.RETAIN_AS_PUBLISHED_FLAG)
        retain_handling = RetainHandling.get((options & cls.RETAIN_HANDLING_MASK) >> 4)
        return data, Subscription(
            pattern=pattern,
            max_qos=qos,
            no_local=no_local,
            retain_as_published=retain_as_published,
            retain_handling=retain_handling,
        )

    def encode(self, buffer: bytearray) -> None:
        encode_utf8(self.pattern, buffer)
        options = self.max_qos | self.retain_handling << 4
        if self.no_local:
            options |= self.NO_LOCAL_FLAG

        if self.retain_as_published:
            options |= self.RETAIN_AS_PUBLISHED_FLAG

        encode_fixed_integer(options, buffer, 1)

    def matches(self, publish: MQTTPublishPacket) -> bool:
        """
        Check if a published message matches this subscription.

        :param publish: an MQTT ``PUBLISH`` packet
        :return: ``True`` if the published message matches this pattern, ``False`` if
            not

        """
        # Don't match if the message's QoS is higher than the accepted maximum in this
        # subscription
        if publish.qos > self.max_qos:
            return False

        # Check that the topic filter matches the message's topic
        topic_parts = publish.topic.split("/")
        for i, (pattern_part, topic_part) in enumerate(
            zip_longest(self._parts, topic_parts)
        ):
            if i or not topic_part.startswith("$"):
                if pattern_part == "#":
                    # MQTT-4.7.2-1
                    return True
                elif pattern_part == "+" and topic_part is not None:
                    # MQTT-4.7.2-1
                    continue

            if topic_part != pattern_part:
                return False

        return True


class MQTTPacket(metaclass=ABCMeta):
    """Abstract base class for all MQTT packets"""

    reserved_flags_mask: ClassVar[int] = 15
    expected_reserved_bits: ClassVar[int] = 0
    packet_type: ClassVar[ControlPacketType]

    __slots__ = ()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        assert isinstance(cls.packet_type, ControlPacketType)
        packet_types[cls.packet_type] = cls

    def encode_fixed_header(
        self, flags: int, payload: bytes, buffer: bytearray
    ) -> None:
        assert flags < 16
        encode_fixed_integer(flags | (self.packet_type << 4), buffer, 1)
        encode_variable_integer(len(payload), buffer)
        buffer.extend(payload)

    @classmethod
    @abstractmethod
    def decode(cls, data: memoryview, flags: int) -> tuple[memoryview, Self]:
        pass

    @abstractmethod
    def encode(self, buffer: bytearray) -> None:
        pass


@define(kw_only=True)
class MQTTConnectPacket(MQTTPacket, PropertiesMixin):
    """Connection request"""

    packet_type = ControlPacketType.CONNECT
    reserved_flags_mask = 0
    allowed_property_types = frozenset(
        [
            PropertyType.SESSION_EXPIRY_INTERVAL,
            PropertyType.AUTHENTICATION_METHOD,
            PropertyType.AUTHENTICATION_DATA,
            PropertyType.REQUEST_RESPONSE_INFORMATION,
            PropertyType.REQUEST_PROBLEM_INFORMATION,
            PropertyType.RECEIVE_MAXIMUM,
            PropertyType.TOPIC_ALIAS_MAXIMUM,
            PropertyType.USER_PROPERTY,
            PropertyType.MAXIMUM_PACKET_SIZE,
        ]
    )

    # Connect flags
    CLEAN_START_FLAG = 2
    WILL_FLAG = 4
    WILL_QOS_MASK = 24
    WILL_RETAIN_FLAG = 32
    PASSWORD_FLAG = 64
    USERNAME_FLAG = 128

    client_id: str
    will: Will | None = None
    username: str | None = None
    password: str | None = None
    clean_start: bool = False
    keep_alive: int = 0

    @classmethod
    def decode(
        cls, data: memoryview, flags: int
    ) -> tuple[memoryview, MQTTConnectPacket]:
        # Decode the variable header
        data, protocol_name = decode_utf8(data)
        if protocol_name != "MQTT":
            raise MQTTProtocolError(f"unexpected protocol: {protocol_name}")

        data, protocol_version = decode_fixed_integer(data, 1)
        if protocol_version != 5:
            raise MQTTProtocolError(f"unsupported protocol version: {protocol_version}")

        data, connect_flags = decode_fixed_integer(data, 1)
        clean_start = bool(connect_flags & cls.CLEAN_START_FLAG)
        data, keep_alive = decode_fixed_integer(data, 2)
        data, properties, user_properties = cls.decode_properties(data)

        # Decode the payload
        data, client_id = decode_utf8(data)

        will: Will | None = None
        if connect_flags & cls.WILL_FLAG:
            data, will_properties, will_user_properties = Will.decode_properties(data)
            data, will_topic = decode_utf8(data)
            payload: bytes | str
            if will_properties.pop(PropertyType.PAYLOAD_FORMAT_INDICATOR, 0):
                data, payload = decode_utf8(data)
            else:
                data, payload = decode_binary(data)

            will = Will(
                topic=will_topic,
                payload=payload,
                retain=bool(connect_flags & cls.WILL_RETAIN_FLAG),
                qos=QoS.get((connect_flags & cls.WILL_QOS_MASK) >> 3),
                properties=will_properties,
                user_properties=will_user_properties,
            )

        username: str | None = None
        if connect_flags & cls.USERNAME_FLAG:
            data, username = decode_utf8(data)

        password: str | None = None
        if connect_flags & cls.PASSWORD_FLAG:
            data, password = decode_utf8(data)

        return data, MQTTConnectPacket(
            client_id=client_id,
            will=will,
            username=username,
            password=password,
            clean_start=clean_start,
            keep_alive=keep_alive,
            properties=properties,
            user_properties=user_properties,
        )

    def encode(self, buffer: bytearray) -> None:
        internal_buffer = bytearray()

        # Gather the flags for the variable header
        connect_flags = 0
        if self.clean_start:
            connect_flags |= self.CLEAN_START_FLAG

        if self.will:
            connect_flags |= self.WILL_FLAG | (self.will.qos << 3)
            if self.will.retain:
                connect_flags |= self.WILL_RETAIN_FLAG

        if self.username is not None:
            connect_flags |= self.USERNAME_FLAG

        if self.password is not None:
            connect_flags |= self.PASSWORD_FLAG

        # Encode the variable header
        encode_utf8("MQTT", internal_buffer)
        encode_fixed_integer(5, internal_buffer, 1)
        encode_fixed_integer(connect_flags, internal_buffer, 1)
        encode_fixed_integer(self.keep_alive, internal_buffer, 2)
        self.encode_properties(internal_buffer)

        # Encode the payload
        encode_utf8(self.client_id, internal_buffer)
        if self.will:
            self.will.encode_properties(internal_buffer)
            encode_utf8(self.will.topic, internal_buffer)
            if isinstance(self.will.payload, str):
                encode_utf8(self.will.payload, internal_buffer)
            else:
                encode_binary(self.will.payload, internal_buffer)

        if self.username is not None:
            encode_utf8(self.username, internal_buffer)

        if self.password is not None:
            encode_utf8(self.password, internal_buffer)

        self.encode_fixed_header(0, internal_buffer, buffer)


@define(kw_only=True)
class MQTTConnAckPacket(MQTTPacket, PropertiesMixin, ReasonCodeMixin):
    """Connection acknowledgment"""

    packet_type = ControlPacketType.CONNACK
    allowed_property_types = frozenset(
        [
            PropertyType.SESSION_EXPIRY_INTERVAL,
            PropertyType.ASSIGNED_CLIENT_IDENTIFIER,
            PropertyType.SERVER_KEEP_ALIVE,
            PropertyType.AUTHENTICATION_METHOD,
            PropertyType.AUTHENTICATION_DATA,
            PropertyType.RESPONSE_INFORMATION,
            PropertyType.SERVER_REFERENCE,
            PropertyType.REASON_STRING,
            PropertyType.RECEIVE_MAXIMUM,
            PropertyType.TOPIC_ALIAS_MAXIMUM,
            PropertyType.MAXIMUM_QOS,
            PropertyType.RETAIN_AVAILABLE,
            PropertyType.USER_PROPERTY,
            PropertyType.MAXIMUM_PACKET_SIZE,
            PropertyType.WILDCARD_SUBSCRIPTION_AVAILABLE,
            PropertyType.SUBSCRIPTION_IDENTIFIER_AVAILABLE,
            PropertyType.SHARED_SUBSCRIPTION_AVAILABLE,
        ]
    )
    allowed_reason_codes = frozenset(
        [
            ReasonCode.SUCCESS,
            ReasonCode.UNSPECIFIED_ERROR,
            ReasonCode.MALFORMED_PACKET,
            ReasonCode.PROTOCOL_ERROR,
            ReasonCode.IMPLEMENTATION_SPECIFIC_ERROR,
            ReasonCode.UNSUPPORTED_PROTOCOL_VERSION,
            ReasonCode.CLIENT_IDENTIFIER_NOT_VALID,
            ReasonCode.BAD_USERNAME_OR_PASSWORD,
            ReasonCode.NOT_AUTHORIZED,
            ReasonCode.SERVER_UNAVAILABLE,
            ReasonCode.SERVER_BUSY,
            ReasonCode.BANNED,
            ReasonCode.BAD_AUTHENTICATION_METHOD,
            ReasonCode.TOPIC_NAME_INVALID,
            ReasonCode.PACKET_TOO_LARGE,
            ReasonCode.QUOTA_EXCEEDED,
            ReasonCode.PAYLOAD_FORMAT_INVALID,
            ReasonCode.RETAIN_NOT_SUPPORTED,
            ReasonCode.QOS_NOT_SUPPORTED,
            ReasonCode.USE_ANOTHER_SERVER,
            ReasonCode.SERVER_MOVED,
            ReasonCode.CONNECTION_RATE_EXCEEDED,
        ]
    )

    SESSION_PRESENT_FLAG = 1

    reason_code: ReasonCode = field(
        validator=[instance_of(ReasonCode), in_(allowed_reason_codes)]
    )
    session_present: bool

    @classmethod
    def decode(
        cls, data: memoryview, flags: int
    ) -> tuple[memoryview, MQTTConnAckPacket]:
        data, connect_ack_flags = decode_fixed_integer(data, 1)
        data, reason_code = cls.decode_reason_code(data)
        data, properties, user_properties = cls.decode_properties(data)
        return data, MQTTConnAckPacket(
            session_present=bool(connect_ack_flags & cls.SESSION_PRESENT_FLAG),
            reason_code=reason_code,
            properties=properties,
            user_properties=user_properties,
        )

    def encode(self, buffer: bytearray) -> None:
        internal_buffer = bytearray()
        connect_flags = int(self.session_present)
        encode_fixed_integer(connect_flags, internal_buffer, 1)
        encode_fixed_integer(self.reason_code, internal_buffer, 1)
        self.encode_properties(internal_buffer)
        self.encode_fixed_header(0, internal_buffer, buffer)


@define(kw_only=True)
class MQTTPublishPacket(MQTTPacket, PropertiesMixin):
    """Publish message"""

    packet_type = ControlPacketType.PUBLISH
    reserved_flags_mask = 0
    allowed_property_types = frozenset(
        [
            PropertyType.PAYLOAD_FORMAT_INDICATOR,
            PropertyType.MESSAGE_EXPIRY_INTERVAL,
            PropertyType.CONTENT_TYPE,
            PropertyType.RESPONSE_TOPIC,
            PropertyType.CORRELATION_DATA,
            PropertyType.SUBSCRIPTION_IDENTIFIER,
            PropertyType.TOPIC_ALIAS,
            PropertyType.USER_PROPERTY,
        ]
    )

    RETAIN_FLAG = 1
    QOS_MASK = 6
    DUP_FLAG = 8

    topic: str
    payload: bytes | str
    packet_id: int | None = field(default=None)
    retain: bool = field(default=False)
    qos: QoS = field(default=QoS.AT_MOST_ONCE)
    duplicate: bool = field(default=False)
    ack_state: PublishAckState = field(default=PublishAckState.UNACKNOWLEDGED)

    def __attrs_post_init__(self) -> None:
        if self.qos and self.packet_id is None:
            raise ValueError("packet_id must be an integer when qos > 0")

        if isinstance(self.payload, str):
            self.properties[PropertyType.PAYLOAD_FORMAT_INDICATOR] = True

    @classmethod
    def decode(
        cls, data: memoryview, flags: int
    ) -> tuple[memoryview, MQTTPublishPacket]:
        # Decode the fixed header flags
        retain = bool(flags & cls.RETAIN_FLAG)
        qos = QoS.get((flags & cls.QOS_MASK) >> 1)
        duplicate = bool(flags & cls.DUP_FLAG)

        # Decode the variable header
        data, topic = decode_utf8(data)
        if qos:
            data, packet_id = decode_fixed_integer(data, 2)
        else:
            packet_id = None

        data, properties, user_properties = cls.decode_properties(data)

        # Decode the payload
        payload: bytes | str
        if properties.pop(PropertyType.PAYLOAD_FORMAT_INDICATOR, 0):
            try:
                data, payload = memoryview(b""), data.tobytes().decode("utf-8")
            except UnicodeDecodeError as exc:
                raise MQTTDecodeError(f"error decoding utf-8 string: {exc}") from None
        else:
            data, payload = memoryview(b""), data.tobytes()

        return data, MQTTPublishPacket(
            topic=topic,
            payload=payload,
            packet_id=packet_id,
            qos=qos,
            retain=retain,
            duplicate=duplicate,
            properties=properties,
            user_properties=user_properties,
        )

    def encode(self, buffer: bytearray) -> None:
        internal_buffer = bytearray()

        # Encode the variable header
        encode_utf8(self.topic, internal_buffer)
        if self.packet_id is not None:
            encode_fixed_integer(self.packet_id, internal_buffer, 2)

        self.encode_properties(internal_buffer)

        # Encode the payload
        if isinstance(self.payload, str):
            internal_buffer.extend(self.payload.encode("utf-8"))
        else:
            internal_buffer.extend(self.payload)

        # Encode the fixed header
        flags = int(self.retain) | self.qos << 1 | self.duplicate << 3
        self.encode_fixed_header(flags, internal_buffer, buffer)


@define(kw_only=True)
class MQTTPublishAckPacket(MQTTPacket, PropertiesMixin, ReasonCodeMixin):
    """Publish acknowledgment (QoS 1)"""

    packet_type = ControlPacketType.PUBACK
    allowed_property_types = frozenset(
        [
            PropertyType.REASON_STRING,
            PropertyType.USER_PROPERTY,
        ]
    )
    allowed_reason_codes = frozenset(
        [
            ReasonCode.SUCCESS,
            ReasonCode.NO_MATCHING_SUBSCRIBERS,
            ReasonCode.UNSPECIFIED_ERROR,
            ReasonCode.IMPLEMENTATION_SPECIFIC_ERROR,
            ReasonCode.NOT_AUTHORIZED,
            ReasonCode.TOPIC_NAME_INVALID,
            ReasonCode.PACKET_IDENTIFIER_IN_USE,
            ReasonCode.QUOTA_EXCEEDED,
            ReasonCode.PAYLOAD_FORMAT_INVALID,
        ]
    )

    packet_id: int
    reason_code: ReasonCode = field(
        validator=[instance_of(ReasonCode), in_(allowed_reason_codes)]
    )

    @classmethod
    def decode(
        cls, data: memoryview, flags: int
    ) -> tuple[memoryview, MQTTPublishAckPacket]:
        # Decode the variable header
        reason_code = ReasonCode.SUCCESS
        properties: dict[PropertyType, PropertyValue] = {}
        user_properties: dict[str, str] = {}

        data, packet_id = decode_fixed_integer(data, 2)
        if data:
            data, reason_code = cls.decode_reason_code(data)
            if data:
                data, properties, user_properties = cls.decode_properties(data)

        return data, MQTTPublishAckPacket(
            packet_id=packet_id,
            reason_code=reason_code,
            properties=properties,
            user_properties=user_properties,
        )

    def encode(self, buffer: bytearray) -> None:
        internal_buffer = bytearray()

        # Encode the variable header
        encode_fixed_integer(self.packet_id, internal_buffer, 2)
        encode_fixed_integer(self.reason_code, internal_buffer, 1)

        self.encode_properties(internal_buffer)

        # Encode the fixed header
        self.encode_fixed_header(0, internal_buffer, buffer)


@define(kw_only=True)
class MQTTPublishReceivePacket(MQTTPacket, PropertiesMixin, ReasonCodeMixin):
    """Publish received (QoS 2 delivery part 1)"""

    packet_type = ControlPacketType.PUBREC
    allowed_property_types = frozenset(
        [
            PropertyType.REASON_STRING,
            PropertyType.USER_PROPERTY,
        ]
    )
    allowed_reason_codes = frozenset(
        [
            ReasonCode.SUCCESS,
            ReasonCode.NO_MATCHING_SUBSCRIBERS,
            ReasonCode.UNSPECIFIED_ERROR,
            ReasonCode.IMPLEMENTATION_SPECIFIC_ERROR,
            ReasonCode.NOT_AUTHORIZED,
            ReasonCode.TOPIC_NAME_INVALID,
            ReasonCode.PACKET_IDENTIFIER_IN_USE,
            ReasonCode.QUOTA_EXCEEDED,
            ReasonCode.PAYLOAD_FORMAT_INVALID,
        ]
    )

    packet_id: int
    reason_code: ReasonCode = field(
        validator=[instance_of(ReasonCode), in_(allowed_reason_codes)]
    )

    @classmethod
    def decode(
        cls, data: memoryview, flags: int
    ) -> tuple[memoryview, MQTTPublishReceivePacket]:
        # Decode the variable header
        reason_code = ReasonCode.SUCCESS
        properties: dict[PropertyType, PropertyValue] = {}
        user_properties: dict[str, str] = {}

        data, packet_id = decode_fixed_integer(data, 2)
        if data:
            data, reason_code = cls.decode_reason_code(data)
            if data:
                data, properties, user_properties = cls.decode_properties(data)

        return data, MQTTPublishReceivePacket(
            packet_id=packet_id,
            reason_code=reason_code,
            properties=properties,
            user_properties=user_properties,
        )

    def encode(self, buffer: bytearray) -> None:
        internal_buffer = bytearray()

        # Encode the variable header
        encode_fixed_integer(self.packet_id, internal_buffer, 2)
        encode_fixed_integer(self.reason_code, internal_buffer, 1)

        self.encode_properties(internal_buffer)

        # Encode the fixed header
        self.encode_fixed_header(0, internal_buffer, buffer)


@define(kw_only=True)
class MQTTPublishReleasePacket(MQTTPacket, PropertiesMixin, ReasonCodeMixin):
    """Publish release (QoS 2 delivery part 2)"""

    packet_type = ControlPacketType.PUBREL
    expected_reserved_bits = 2
    allowed_property_types = frozenset(
        [
            PropertyType.REASON_STRING,
            PropertyType.USER_PROPERTY,
        ]
    )
    allowed_reason_codes = frozenset(
        [ReasonCode.SUCCESS, ReasonCode.PACKET_IDENTIFIER_NOT_FOUND]
    )

    packet_id: int
    reason_code: ReasonCode = field(
        validator=[instance_of(ReasonCode), in_(allowed_reason_codes)]
    )

    @classmethod
    def decode(
        cls, data: memoryview, flags: int
    ) -> tuple[memoryview, MQTTPublishReleasePacket]:
        # Decode the variable header
        reason_code = ReasonCode.SUCCESS
        properties: dict[PropertyType, PropertyValue] = {}
        user_properties: dict[str, str] = {}

        data, packet_id = decode_fixed_integer(data, 2)
        if data:
            data, reason_code = cls.decode_reason_code(data)
            if data:
                data, properties, user_properties = cls.decode_properties(data)

        return data, MQTTPublishReleasePacket(
            packet_id=packet_id,
            reason_code=reason_code,
            properties=properties,
            user_properties=user_properties,
        )

    def encode(self, buffer: bytearray) -> None:
        internal_buffer = bytearray()

        # Encode the variable header
        encode_fixed_integer(self.packet_id, internal_buffer, 2)
        encode_fixed_integer(self.reason_code, internal_buffer, 1)

        self.encode_properties(internal_buffer)

        # Encode the fixed header
        self.encode_fixed_header(self.expected_reserved_bits, internal_buffer, buffer)


@define(kw_only=True)
class MQTTPublishCompletePacket(MQTTPacket, PropertiesMixin, ReasonCodeMixin):
    """Publish complete (QoS 2 delivery part 3)"""

    packet_type = ControlPacketType.PUBCOMP
    allowed_property_types = frozenset(
        [
            PropertyType.REASON_STRING,
            PropertyType.USER_PROPERTY,
        ]
    )
    allowed_reason_codes = frozenset(
        [ReasonCode.SUCCESS, ReasonCode.PACKET_IDENTIFIER_NOT_FOUND]
    )

    packet_id: int
    reason_code: ReasonCode = field(
        validator=[instance_of(ReasonCode), in_(allowed_reason_codes)]
    )

    @classmethod
    def decode(
        cls, data: memoryview, flags: int
    ) -> tuple[memoryview, MQTTPublishCompletePacket]:
        # Decode the variable header
        reason_code = ReasonCode.SUCCESS
        properties: dict[PropertyType, PropertyValue] = {}
        user_properties: dict[str, str] = {}

        data, packet_id = decode_fixed_integer(data, 2)
        if data:
            data, reason_code = cls.decode_reason_code(data)
            if data:
                data, properties, user_properties = cls.decode_properties(data)

        return data, MQTTPublishCompletePacket(
            packet_id=packet_id,
            reason_code=reason_code,
            properties=properties,
            user_properties=user_properties,
        )

    def encode(self, buffer: bytearray) -> None:
        internal_buffer = bytearray()

        # Encode the variable header
        encode_fixed_integer(self.packet_id, internal_buffer, 2)
        encode_fixed_integer(self.reason_code, internal_buffer, 1)

        self.encode_properties(internal_buffer)

        # Encode the fixed header
        self.encode_fixed_header(0, internal_buffer, buffer)


@define(kw_only=True)
class MQTTSubscribePacket(MQTTPacket, PropertiesMixin):
    """Subscribe request"""

    expected_reserved_bits: ClassVar[int] = 2

    packet_type = ControlPacketType.SUBSCRIBE
    allowed_property_types = frozenset(
        [
            PropertyType.SUBSCRIPTION_IDENTIFIER,
            PropertyType.USER_PROPERTY,
        ]
    )

    packet_id: int
    subscriptions: Sequence[Subscription]

    def __attrs_post_init__(self) -> None:
        if not self.subscriptions:
            raise MQTTProtocolError("subscription must have at least one topic filter")

    @classmethod
    def decode(
        cls, data: memoryview, flags: int
    ) -> tuple[memoryview, MQTTSubscribePacket]:
        # Decode the variable header
        data, packet_id = decode_fixed_integer(data, 2)
        data, properties, user_properties = cls.decode_properties(data)

        # Decode the payload
        subscriptions: list[Subscription] = []
        while data:
            data, subscription = Subscription.decode(data)
            subscriptions.append(subscription)

        return data, MQTTSubscribePacket(
            packet_id=packet_id,
            subscriptions=subscriptions,
            properties=properties,
            user_properties=user_properties,
        )

    def encode(self, buffer: bytearray) -> None:
        internal_buffer = bytearray()

        # Encode the variable header
        encode_fixed_integer(self.packet_id, internal_buffer, 2)
        self.encode_properties(internal_buffer)

        # Encode the payload
        for subscription in self.subscriptions:
            subscription.encode(internal_buffer)

        self.encode_fixed_header(self.expected_reserved_bits, internal_buffer, buffer)


@define(kw_only=True)
class MQTTSubscribeAckPacket(MQTTPacket, PropertiesMixin, ReasonCodeMixin):
    """Subscribe acknowledgment"""

    packet_type = ControlPacketType.SUBACK
    allowed_property_types = frozenset(
        [
            PropertyType.REASON_STRING,
            PropertyType.USER_PROPERTY,
        ]
    )
    allowed_reason_codes = frozenset(
        [
            ReasonCode.GRANTED_QOS_0,
            ReasonCode.GRANTED_QOS_1,
            ReasonCode.GRANTED_QOS_2,
            ReasonCode.UNSPECIFIED_ERROR,
            ReasonCode.IMPLEMENTATION_SPECIFIC_ERROR,
            ReasonCode.NOT_AUTHORIZED,
            ReasonCode.TOPIC_FILTER_INVALID,
            ReasonCode.PACKET_IDENTIFIER_IN_USE,
            ReasonCode.QUOTA_EXCEEDED,
            ReasonCode.SHARED_SUBSCRIPTIONS_NOT_SUPPORTED,
            ReasonCode.SUBSCRIPTION_IDENTIFIERS_NOT_SUPPORTED,
            ReasonCode.WILDCARD_SUBSCRIPTIONS_NOT_SUPPORTED,
        ]
    )

    packet_id: int
    reason_codes: Sequence[ReasonCode] = field(
        validator=deep_iterable([instance_of(ReasonCode), in_(allowed_reason_codes)])
    )

    @classmethod
    def decode(
        cls, data: memoryview, flags: int
    ) -> tuple[memoryview, MQTTSubscribeAckPacket]:
        # Decode the variable header
        data, packet_id = decode_fixed_integer(data, 2)
        data, properties, user_properties = cls.decode_properties(data)

        # Decode the payload
        reason_codes: list[ReasonCode] = []
        while data:
            data, reason_code = cls.decode_reason_code(data)
            reason_codes.append(reason_code)

        return data, MQTTSubscribeAckPacket(
            packet_id=packet_id,
            reason_codes=reason_codes,
            properties=properties,
            user_properties=user_properties,
        )

    def encode(self, buffer: bytearray) -> None:
        internal_buffer = bytearray()

        # Encode the variable header
        encode_fixed_integer(self.packet_id, internal_buffer, 2)
        self.encode_properties(internal_buffer)

        # Encode the payload
        for reason_code in self.reason_codes:
            encode_fixed_integer(reason_code, internal_buffer, 1)

        # Encode the fixed header
        self.encode_fixed_header(0, internal_buffer, buffer)


@define(kw_only=True)
class MQTTUnsubscribePacket(MQTTPacket, PropertiesMixin):
    """Unsubscribe request"""

    packet_type = ControlPacketType.UNSUBSCRIBE
    expected_reserved_bits = 2
    allowed_property_types = frozenset([PropertyType.USER_PROPERTY])

    packet_id: int
    patterns: Sequence[str]

    def __attrs_post_init__(self) -> None:
        if not self.patterns:
            raise MQTTProtocolError(
                "an unsubscribe request must have at least one topic filter"
            )

    @classmethod
    def decode(
        cls, data: memoryview, flags: int
    ) -> tuple[memoryview, MQTTUnsubscribePacket]:
        # Decode the variable header
        data, packet_id = decode_fixed_integer(data, 2)
        data, properties, user_properties = cls.decode_properties(data)

        # Decode the payload
        patterns: list[str] = []
        while data:
            data, pattern = decode_utf8(data)
            patterns.append(pattern)

        return data, MQTTUnsubscribePacket(
            packet_id=packet_id,
            patterns=patterns,
            properties=properties,
            user_properties=user_properties,
        )

    def encode(self, buffer: bytearray) -> None:
        internal_buffer = bytearray()

        # Encode the variable header
        encode_fixed_integer(self.packet_id, internal_buffer, 2)
        self.encode_properties(internal_buffer)

        # Encode the payload
        for pattern in self.patterns:
            encode_utf8(pattern, internal_buffer)

        # Encode the fixed header
        self.encode_fixed_header(self.expected_reserved_bits, internal_buffer, buffer)


@define(kw_only=True)
class MQTTUnsubscribeAckPacket(MQTTPacket, PropertiesMixin, ReasonCodeMixin):
    """Unsubscribe acknowledgment"""

    packet_type = ControlPacketType.UNSUBACK
    allowed_property_types = frozenset(
        [PropertyType.REASON_STRING, PropertyType.USER_PROPERTY]
    )
    allowed_reason_codes = frozenset(
        [
            ReasonCode.SUCCESS,
            ReasonCode.NO_SUBSCRIPTION_EXISTED,
            ReasonCode.UNSPECIFIED_ERROR,
            ReasonCode.IMPLEMENTATION_SPECIFIC_ERROR,
            ReasonCode.NOT_AUTHORIZED,
            ReasonCode.TOPIC_FILTER_INVALID,
            ReasonCode.PACKET_IDENTIFIER_IN_USE,
        ]
    )

    packet_id: int
    reason_codes: Sequence[ReasonCode] = field(
        validator=deep_iterable([instance_of(ReasonCode), in_(allowed_reason_codes)])
    )

    @classmethod
    def decode(
        cls, data: memoryview, flags: int
    ) -> tuple[memoryview, MQTTUnsubscribeAckPacket]:
        # Decode the variable header
        data, packet_id = decode_fixed_integer(data, 2)
        data, properties, user_properties = cls.decode_properties(data)

        # Decode the payload
        reason_codes: list[ReasonCode] = []
        while data:
            data, reason_code = cls.decode_reason_code(data)
            reason_codes.append(reason_code)

        return data, MQTTUnsubscribeAckPacket(
            packet_id=packet_id,
            reason_codes=reason_codes,
            properties=properties,
            user_properties=user_properties,
        )

    def encode(self, buffer: bytearray) -> None:
        internal_buffer = bytearray()

        # Encode the variable header
        encode_fixed_integer(self.packet_id, internal_buffer, 2)
        self.encode_properties(internal_buffer)

        # Encode the payload
        for reason_code in self.reason_codes:
            encode_fixed_integer(reason_code, internal_buffer, 1)

        # Encode the fixed header
        self.encode_fixed_header(0, internal_buffer, buffer)


@define(kw_only=True)
class MQTTPingRequestPacket(MQTTPacket):
    """PING request"""

    packet_type = ControlPacketType.PINGREQ

    @classmethod
    def decode(
        cls, data: memoryview, flags: int
    ) -> tuple[memoryview, MQTTPingRequestPacket]:
        return data, MQTTPingRequestPacket()

    def encode(self, buffer: bytearray) -> None:
        # Encode the fixed header
        self.encode_fixed_header(0, b"", buffer)


@define(kw_only=True)
class MQTTPingResponsePacket(MQTTPacket):
    """PING response"""

    packet_type = ControlPacketType.PINGRESP

    @classmethod
    def decode(
        cls, data: memoryview, flags: int
    ) -> tuple[memoryview, MQTTPingResponsePacket]:
        return data, MQTTPingResponsePacket()

    def encode(self, buffer: bytearray) -> None:
        # Encode the fixed header
        self.encode_fixed_header(0, b"", buffer)


@define(kw_only=True)
class MQTTDisconnectPacket(MQTTPacket, PropertiesMixin, ReasonCodeMixin):
    """Disconnect notification"""

    packet_type = ControlPacketType.DISCONNECT
    allowed_property_types = frozenset(
        [
            PropertyType.SESSION_EXPIRY_INTERVAL,
            PropertyType.SERVER_REFERENCE,
            PropertyType.REASON_STRING,
            PropertyType.USER_PROPERTY,
        ]
    )
    allowed_reason_codes = frozenset(
        [
            ReasonCode.NORMAL_DISCONNECTION,
            ReasonCode.DISCONNECT_WITH_WILL_MESSAGE,
            ReasonCode.UNSPECIFIED_ERROR,
            ReasonCode.MALFORMED_PACKET,
            ReasonCode.PROTOCOL_ERROR,
            ReasonCode.IMPLEMENTATION_SPECIFIC_ERROR,
            ReasonCode.NOT_AUTHORIZED,
            ReasonCode.SERVER_BUSY,
            ReasonCode.SERVER_SHUTTING_DOWN,
            ReasonCode.SESSION_TAKEN_OVER,
            ReasonCode.TOPIC_FILTER_INVALID,
            ReasonCode.TOPIC_NAME_INVALID,
            ReasonCode.RECEIVE_MAXIMUM_EXCEEDED,
            ReasonCode.TOPIC_ALIAS_INVALID,
            ReasonCode.PACKET_TOO_LARGE,
            ReasonCode.MESSAGE_RATE_TOO_HIGH,
            ReasonCode.QUOTA_EXCEEDED,
            ReasonCode.ADMINISTRATIVE_ACTION,
            ReasonCode.PAYLOAD_FORMAT_INVALID,
            ReasonCode.RETAIN_NOT_SUPPORTED,
            ReasonCode.QOS_NOT_SUPPORTED,
            ReasonCode.USE_ANOTHER_SERVER,
            ReasonCode.SERVER_MOVED,
            ReasonCode.SHARED_SUBSCRIPTIONS_NOT_SUPPORTED,
            ReasonCode.CONNECTION_RATE_EXCEEDED,
            ReasonCode.MAXIMUM_CONNECT_TIME,
            ReasonCode.SUBSCRIPTION_IDENTIFIERS_NOT_SUPPORTED,
            ReasonCode.WILDCARD_SUBSCRIPTIONS_NOT_SUPPORTED,
        ]
    )

    reason_code: ReasonCode = field(
        validator=[instance_of(ReasonCode), in_(allowed_reason_codes)]
    )

    @classmethod
    def decode(
        cls, data: memoryview, flags: int
    ) -> tuple[memoryview, MQTTDisconnectPacket]:
        # Decode the variable header
        reason_code = ReasonCode.SUCCESS
        properties: dict[PropertyType, PropertyValue] = {}
        user_properties: dict[str, str] = {}

        if data:
            data, reason_code = cls.decode_reason_code(data)
            if data:
                data, properties, user_properties = cls.decode_properties(data)

        return data, MQTTDisconnectPacket(
            reason_code=reason_code,
            properties=properties,
            user_properties=user_properties,
        )

    def encode(self, buffer: bytearray) -> None:
        internal_buffer = bytearray()

        # Encode the variable header
        encode_fixed_integer(self.reason_code, internal_buffer, 1)
        self.encode_properties(internal_buffer)

        # Encode the fixed header
        self.encode_fixed_header(0, internal_buffer, buffer)


@define(kw_only=True)
class MQTTAuthPacket(MQTTPacket, PropertiesMixin, ReasonCodeMixin):
    """Authentication exchange"""

    packet_type = ControlPacketType.AUTH
    allowed_property_types = frozenset(
        [
            PropertyType.AUTHENTICATION_METHOD,
            PropertyType.AUTHENTICATION_DATA,
            PropertyType.REASON_STRING,
            PropertyType.USER_PROPERTY,
        ]
    )
    allowed_reason_codes = frozenset(
        [
            ReasonCode.SUCCESS,
            ReasonCode.CONTINUE_AUTHENTICATION,
            ReasonCode.REAUTHENTICATE,
        ]
    )

    reason_code: ReasonCode = field(
        validator=[instance_of(ReasonCode), in_(allowed_reason_codes)]
    )

    @classmethod
    def decode(cls, data: memoryview, flags: int) -> tuple[memoryview, MQTTAuthPacket]:
        # Decode the variable header
        data, reason_code = cls.decode_reason_code(data)
        data, properties, user_properties = cls.decode_properties(data)

        return data, MQTTAuthPacket(
            reason_code=reason_code,
            properties=properties,
            user_properties=user_properties,
        )

    def encode(self, buffer: bytearray) -> None:
        internal_buffer = bytearray()

        # Encode the variable header
        encode_fixed_integer(self.reason_code, internal_buffer, 1)
        self.encode_properties(internal_buffer)

        # Encode the fixed header
        self.encode_fixed_header(0, internal_buffer, buffer)


def decode_packet(
    data: memoryview, max_packet_size: int | None = None
) -> tuple[memoryview, MQTTPacket]:
    if len(data) < 2:
        raise InsufficientData

    packet_type_num = data[0] >> 4
    flags = data[0] & 15
    try:
        packet_type = packet_types[packet_type_num].packet_type
    except KeyError:
        raise MQTTProtocolError(
            f"unrecognized packet type: {packet_type_num}"
        ) from None

    previous_length = data.nbytes
    data, remaining_length = decode_variable_integer(data[1:])

    if max_packet_size is not None:
        # Check if the packet is larger than the maximum
        packet_size = remaining_length + data.nbytes - previous_length
        if packet_size > max_packet_size:
            raise MQTTPacketTooLarge

    if len(data) < remaining_length:
        raise InsufficientData

    packet_cls = packet_types[packet_type]
    if flags & packet_cls.reserved_flags_mask != packet_cls.expected_reserved_bits:
        raise MQTTProtocolError(
            f"received {packet_type._name_} with reserved bits set in its fixed header"
        )

    try:
        leftover_data, packet = packet_cls.decode(data[:remaining_length], flags)
    except InsufficientData as exc:
        # this is a problem with our code, not an MQTT error
        raise RuntimeError(
            f"decoding {packet_type._name_} consumed more data than available"
        ) from exc

    if leftover_data:
        raise MQTTDecodeError(
            f"not all data was consumed when decoding a {packet_type._name_} packet"
        )

    return data[remaining_length:], packet
