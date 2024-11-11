from __future__ import annotations

import pytest

from mqttproto._exceptions import (
    InsufficientData,
    InvalidPattern,
    MQTTDecodeError,
    MQTTPacketTooLarge,
)
from mqttproto._types import (
    MQTTAuthPacket,
    MQTTConnAckPacket,
    MQTTConnectPacket,
    MQTTDisconnectPacket,
    MQTTPingRequestPacket,
    MQTTPingResponsePacket,
    MQTTPublishAckPacket,
    MQTTPublishCompletePacket,
    MQTTPublishPacket,
    MQTTPublishReceivePacket,
    MQTTPublishReleasePacket,
    MQTTSubscribeAckPacket,
    MQTTSubscribePacket,
    MQTTUnsubscribeAckPacket,
    MQTTUnsubscribePacket,
    PropertyType,
    PropertyValue,
    QoS,
    ReasonCode,
    RetainHandling,
    Subscription,
    Will,
    decode_binary,
    decode_fixed_integer,
    decode_packet,
    decode_utf8,
    decode_variable_integer,
    encode_binary,
    encode_fixed_integer,
    encode_utf8,
    encode_variable_integer,
)


class TestFixedInteger:
    @pytest.mark.parametrize(
        "value, size",
        [
            pytest.param(0, 1, id="0"),
            pytest.param(255, 1, id="255"),
            pytest.param(256, 2, id="255"),
            pytest.param(65535, 2, id="65535"),
            pytest.param(4294967295, 4, id="4294967295"),
        ],
    )
    def test_roundtrip(self, value: int, size: int) -> None:
        buffer = bytearray()
        encode_fixed_integer(value, buffer, size)
        for length in range(1, len(buffer)):
            with pytest.raises(InsufficientData):
                decode_fixed_integer(memoryview(buffer)[:length], size)

        data, decoded = decode_fixed_integer(memoryview(buffer), size)
        assert not data
        assert decoded == value


class TestDecodeVariableInteger:
    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(0, id="0"),
            pytest.param(255, id="255"),
            pytest.param(256, id="255"),
            pytest.param(65535, id="65535"),
            pytest.param(4294967295, id="4294967295"),
        ],
    )
    def test_roundtrip(self, value: int) -> None:
        buffer = bytearray()
        encode_variable_integer(value, buffer)
        for length in range(1, len(buffer)):
            with pytest.raises(InsufficientData):
                decode_variable_integer(memoryview(buffer)[:length])

        data, decoded = decode_variable_integer(memoryview(buffer))
        assert not data
        assert decoded == value


class TestBinary:
    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(b"", id="empty"),
            pytest.param(b"\x00\xff\x00\xff", id="full"),
        ],
    )
    def test_roundtrip(self, value: bytes) -> None:
        buffer = bytearray()
        encode_binary(value, buffer)
        for length in range(1, len(buffer)):
            with pytest.raises(InsufficientData):
                decode_binary(memoryview(buffer)[:length])

        data, decoded = decode_binary(memoryview(buffer))
        assert not data
        assert decoded == value


class TestUTF8:
    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("", id="empty"),
            pytest.param("åäöabcåäö", id="unicode"),
        ],
    )
    def test_roundtrip(self, value: str) -> None:
        buffer = bytearray()
        encode_utf8(value, buffer)
        for length in range(1, len(buffer)):
            with pytest.raises(InsufficientData):
                decode_utf8(memoryview(buffer)[:length])

        data, decoded = decode_utf8(memoryview(buffer))
        assert not data
        assert decoded == value

    def test_invalid_utf8(self) -> None:
        buffer = bytearray()
        encode_fixed_integer(1, buffer, 2)
        buffer.append(0xFF)
        with pytest.raises(
            MQTTDecodeError,
            match=(
                "error decoding utf-8 string: 'utf-8' codec can't decode byte 0xff in "
                "position 0"
            ),
        ):
            decode_utf8(memoryview(buffer))


def test_unknown_reason_code() -> None:
    with pytest.raises(MQTTDecodeError, match="unknown reason code: 0xFF"):
        ReasonCode.get(0xFF)


def test_unknown_retain_handling() -> None:
    with pytest.raises(MQTTDecodeError, match="unknown retain handling: 0xFF"):
        RetainHandling.get(0xFF)


def test_unknown_qos() -> None:
    with pytest.raises(MQTTDecodeError, match="unknown QoS value: 0xFF"):
        QoS.get(0xFF)


def test_unknown_property_type() -> None:
    with pytest.raises(MQTTDecodeError, match="unknown property type: 0xFF"):
        PropertyType.get(0xFF)


def test_packet_too_large() -> None:
    buffer = bytearray()
    MQTTPublishPacket(topic="foo", payload="bar").encode(buffer)
    with pytest.raises(MQTTPacketTooLarge):
        decode_packet(memoryview(buffer), 8)


def test_decode_two_packets_from_buffer() -> None:
    buffer = bytearray()
    MQTTSubscribeAckPacket(
        reason_codes=[ReasonCode.GRANTED_QOS_0, ReasonCode.GRANTED_QOS_1], packet_id=1
    ).encode(buffer)
    MQTTPublishPacket(topic="foo", payload="bar").encode(buffer)

    view = memoryview(buffer)
    view, packet1 = decode_packet(view)
    view, packet2 = decode_packet(view)
    assert not view
    assert isinstance(packet1, MQTTSubscribeAckPacket)
    assert isinstance(packet2, MQTTPublishPacket)


class TestSubscription:
    @pytest.mark.parametrize("pattern", ["#", "foo/+", "+/bar", "+/+", "foo/bar/#"])
    def test_topic_matches(self, pattern: str) -> None:
        publish = MQTTPublishPacket(topic="foo/bar", payload="")
        assert Subscription(pattern).matches(publish)

    @pytest.mark.parametrize("pattern", ["foo/bar/baz", "foo", "foo/bar/+"])
    def test_topic_no_match(self, pattern: str) -> None:
        publish = MQTTPublishPacket(topic="foo/bar", payload="")
        assert not Subscription(pattern).matches(publish)

    def test_single_level_wildcard_not_alone(self) -> None:
        with pytest.raises(
            InvalidPattern,
            match=r"single-level wildcard \('\+'\) must occupy an entire level of the topic filter",
        ):
            Subscription("foo/+bar/baz")

    def test_multi_level_wildcard_not_alone(self) -> None:
        with pytest.raises(
            InvalidPattern,
            match=r"multi-level wildcard \('\#'\) must occupy an entire level of the topic filter",
        ):
            Subscription("foo/#bar/baz")

    def test_multi_level_wildcard_not_at_end(self) -> None:
        with pytest.raises(
            InvalidPattern,
            match=r"multi-level wildcard \('#'\) must be the last character in the topic filter",
        ):
            Subscription(pattern="foo/#/baz")

    @pytest.mark.parametrize("pattern", ["#", "+/foo/bar"])
    def test_sys_topic_wildcard(self, pattern: str) -> None:
        publish = MQTTPublishPacket(topic="$SYS/foo/bar", payload="")
        assert not Subscription(pattern).matches(publish)

    def test_group_id(self) -> None:
        assert Subscription(pattern="$share/foo/bar").group_id == "foo"


class TestMQTTConnectPacket:
    def test_minimal(self) -> None:
        packet = MQTTConnectPacket(client_id="teståäö")
        buffer = bytearray()
        packet.encode(buffer)

        leftover_data, packet2 = decode_packet(memoryview(buffer))
        assert packet2 == packet
        assert not leftover_data

    @pytest.mark.parametrize(
        "will_payload",
        [
            pytest.param("payläad", id="utf8"),
            pytest.param(b"payload", id="binary"),
        ],
    )
    def test_full(self, will_payload: str | bytes) -> None:
        will_properties: dict[PropertyType, PropertyValue] = {
            PropertyType.MESSAGE_EXPIRY_INTERVAL: 15,
            PropertyType.CONTENT_TYPE: "application/json",
            PropertyType.RESPONSE_TOPIC: "res/pon/se",
            PropertyType.CORRELATION_DATA: b"random",
        }
        will_user_properties = {"test1": "foo", "test2": "bar"}
        will = Will(
            topic="will_töpic",
            payload=will_payload,
            retain=True,
            qos=QoS.EXACTLY_ONCE,
            properties=will_properties,
            user_properties=will_user_properties,
        )
        properties: dict[PropertyType, PropertyValue] = {
            PropertyType.SESSION_EXPIRY_INTERVAL: 255
        }
        user_properties = {"foo": "bar", "key2": "value2"}
        packet = MQTTConnectPacket(
            client_id="teståäö",
            will=will,
            username="usernämë",
            password="pässörd",
            clean_start=False,
            keep_alive=65535,
            properties=properties,
            user_properties=user_properties,
        )
        buffer = bytearray()
        packet.encode(buffer)

        leftover_data, packet2 = decode_packet(memoryview(buffer))
        assert packet2 == packet
        assert not leftover_data


class TestMQTTConnAckPacket:
    def test_minimal(self) -> None:
        packet = MQTTConnAckPacket(
            session_present=False, reason_code=ReasonCode.SUCCESS
        )
        buffer = bytearray()
        packet.encode(buffer)

        leftover_data, packet2 = decode_packet(memoryview(buffer))
        assert packet2 == packet
        assert not leftover_data

    def test_full(self) -> None:
        properties: dict[PropertyType, PropertyValue] = {
            PropertyType.SESSION_EXPIRY_INTERVAL: 255,
            PropertyType.ASSIGNED_CLIENT_IDENTIFIER: "foä",
            PropertyType.SERVER_KEEP_ALIVE: 65535,
            PropertyType.AUTHENTICATION_METHOD: "SCRAM-SHA-1",
            PropertyType.AUTHENTICATION_DATA: b"random",
            PropertyType.RESPONSE_INFORMATION: "infö",
            PropertyType.SERVER_REFERENCE: "änöther",
            PropertyType.REASON_STRING: "Bänned",
            PropertyType.RECEIVE_MAXIMUM: 65535,
            PropertyType.TOPIC_ALIAS_MAXIMUM: 65535,
            PropertyType.MAXIMUM_QOS: 2,
            PropertyType.RETAIN_AVAILABLE: True,
            PropertyType.MAXIMUM_PACKET_SIZE: 1000000,
            PropertyType.WILDCARD_SUBSCRIPTION_AVAILABLE: True,
            PropertyType.SUBSCRIPTION_IDENTIFIER_AVAILABLE: True,
            PropertyType.SHARED_SUBSCRIPTION_AVAILABLE: True,
        }
        user_properties = {"foo": "bar", "key2": "value2"}
        packet = MQTTConnAckPacket(
            session_present=True,
            reason_code=ReasonCode.UNSPECIFIED_ERROR,
            properties=properties,
            user_properties=user_properties,
        )
        buffer = bytearray()
        packet.encode(buffer)

        leftover_data, packet2 = decode_packet(memoryview(buffer))
        assert packet2 == packet
        assert not leftover_data

    def test_bad_reason_codes(self) -> None:
        for reason_code in ReasonCode.__members__.values():
            if reason_code not in MQTTConnAckPacket.allowed_reason_codes:
                with pytest.raises(ValueError):
                    MQTTConnAckPacket(reason_code=reason_code, session_present=False)


class TestMQTTPublishPacket:
    def test_minimal(self) -> None:
        packet = MQTTPublishPacket(topic="test/töpic", payload=b"")
        buffer = bytearray()
        packet.encode(buffer)

        leftover_data, packet2 = decode_packet(memoryview(buffer))
        assert packet2 == packet
        assert not leftover_data

    @pytest.mark.parametrize(
        "payload",
        [pytest.param(b"random", id="binary"), pytest.param("teståäö", id="utf8")],
    )
    def test_full(self, payload: bytes | str) -> None:
        properties: dict[PropertyType, PropertyValue] = {
            PropertyType.MESSAGE_EXPIRY_INTERVAL: 15,
            PropertyType.CONTENT_TYPE: "application/json",
            PropertyType.RESPONSE_TOPIC: "res/pon/se",
            PropertyType.CORRELATION_DATA: b"random",
            PropertyType.SUBSCRIPTION_IDENTIFIER: [268_435_455],
            PropertyType.TOPIC_ALIAS: 65535,
        }
        user_properties = {"foo": "bar", "key2": "value2"}
        packet = MQTTPublishPacket(
            topic="tö/p1/c",
            payload=payload,
            retain=True,
            qos=QoS.EXACTLY_ONCE,
            duplicate=True,
            packet_id=65535,
            properties=properties,
            user_properties=user_properties,
        )
        buffer = bytearray()
        packet.encode(buffer)

        leftover_data, packet2 = decode_packet(memoryview(buffer))
        assert packet2 == packet
        assert not leftover_data


class TestMQTTPublishAckPacket:
    def test_minimal(self) -> None:
        packet = MQTTPublishAckPacket(packet_id=1, reason_code=ReasonCode.SUCCESS)
        buffer = bytearray()
        packet.encode(buffer)

        leftover_data, packet2 = decode_packet(memoryview(buffer))
        assert packet2 == packet
        assert not leftover_data

    def test_full(self) -> None:
        properties: dict[PropertyType, PropertyValue] = {
            PropertyType.REASON_STRING: "Bad data",
        }
        user_properties = {"foo": "bar", "key2": "value2"}
        packet = MQTTPublishAckPacket(
            packet_id=65535,
            reason_code=ReasonCode.UNSPECIFIED_ERROR,
            properties=properties,
            user_properties=user_properties,
        )
        buffer = bytearray()
        packet.encode(buffer)

        leftover_data, packet2 = decode_packet(memoryview(buffer))
        assert packet2 == packet
        assert not leftover_data

    def test_partial(self) -> None:
        packet = MQTTPublishAckPacket(packet_id=65534, reason_code=ReasonCode.SUCCESS)
        buffer = bytearray()
        buffer2 = bytearray()
        encode_fixed_integer(packet.packet_id, buffer2, 2)

        packet.encode_fixed_header(0, buffer2, buffer)
        leftover_data, packet2 = decode_packet(memoryview(buffer))
        assert packet2 == packet
        assert not leftover_data

    def test_bad_reason_codes(self) -> None:
        for reason_code in ReasonCode.__members__.values():
            if reason_code not in MQTTPublishAckPacket.allowed_reason_codes:
                with pytest.raises(ValueError):
                    MQTTPublishAckPacket(reason_code=reason_code, packet_id=1)


class TestMQTTPublishReceivePacket:
    def test_minimal(self) -> None:
        packet = MQTTPublishReceivePacket(packet_id=1, reason_code=ReasonCode.SUCCESS)
        buffer = bytearray()
        packet.encode(buffer)

        leftover_data, packet2 = decode_packet(memoryview(buffer))
        assert packet2 == packet
        assert not leftover_data

    def test_full(self) -> None:
        properties: dict[PropertyType, PropertyValue] = {
            PropertyType.REASON_STRING: "Bad data",
        }
        user_properties = {"foo": "bar", "key2": "value2"}
        packet = MQTTPublishReceivePacket(
            packet_id=65535,
            reason_code=ReasonCode.UNSPECIFIED_ERROR,
            properties=properties,
            user_properties=user_properties,
        )
        buffer = bytearray()
        packet.encode(buffer)

        leftover_data, packet2 = decode_packet(memoryview(buffer))
        assert packet2 == packet
        assert not leftover_data

    def test_partial(self) -> None:
        packet = MQTTPublishReceivePacket(
            packet_id=65534, reason_code=ReasonCode.SUCCESS
        )
        buffer = bytearray()
        buffer2 = bytearray()
        encode_fixed_integer(packet.packet_id, buffer2, 2)

        packet.encode_fixed_header(0, buffer2, buffer)
        leftover_data, packet2 = decode_packet(memoryview(buffer))
        assert packet2 == packet
        assert not leftover_data

    def test_bad_reason_codes(self) -> None:
        for reason_code in ReasonCode.__members__.values():
            if reason_code not in MQTTPublishReceivePacket.allowed_reason_codes:
                with pytest.raises(ValueError):
                    MQTTPublishReceivePacket(reason_code=reason_code, packet_id=1)


class TestMQTTPublishReleasePacket:
    def test_minimal(self) -> None:
        packet = MQTTPublishReleasePacket(packet_id=1, reason_code=ReasonCode.SUCCESS)
        buffer = bytearray()
        packet.encode(buffer)

        leftover_data, packet2 = decode_packet(memoryview(buffer))
        assert packet2 == packet
        assert not leftover_data

    def test_full(self) -> None:
        properties: dict[PropertyType, PropertyValue] = {
            PropertyType.REASON_STRING: "Bad data",
        }
        user_properties = {"foo": "bar", "key2": "value2"}
        packet = MQTTPublishReleasePacket(
            packet_id=65535,
            reason_code=ReasonCode.PACKET_IDENTIFIER_NOT_FOUND,
            properties=properties,
            user_properties=user_properties,
        )
        buffer = bytearray()
        packet.encode(buffer)

        leftover_data, packet2 = decode_packet(memoryview(buffer))
        assert packet2 == packet
        assert not leftover_data

    def test_partial(self) -> None:
        packet = MQTTPublishReleasePacket(
            packet_id=65534, reason_code=ReasonCode.SUCCESS
        )
        buffer = bytearray()
        buffer2 = bytearray()
        encode_fixed_integer(packet.packet_id, buffer2, 2)

        packet.encode_fixed_header(2, buffer2, buffer)
        leftover_data, packet2 = decode_packet(memoryview(buffer))
        assert packet2 == packet
        assert not leftover_data

    def test_bad_reason_codes(self) -> None:
        for reason_code in ReasonCode.__members__.values():
            if reason_code not in MQTTPublishReleasePacket.allowed_reason_codes:
                with pytest.raises(ValueError):
                    MQTTPublishReleasePacket(reason_code=reason_code, packet_id=1)


class TestMQTTPublishCompletePacket:
    def test_minimal(self) -> None:
        packet = MQTTPublishCompletePacket(packet_id=1, reason_code=ReasonCode.SUCCESS)
        buffer = bytearray()
        packet.encode(buffer)

        leftover_data, packet2 = decode_packet(memoryview(buffer))
        assert packet2 == packet
        assert not leftover_data

    def test_full(self) -> None:
        properties: dict[PropertyType, PropertyValue] = {
            PropertyType.REASON_STRING: "Bad data",
        }
        user_properties = {"foo": "bar", "key2": "value2"}
        packet = MQTTPublishCompletePacket(
            packet_id=65535,
            reason_code=ReasonCode.PACKET_IDENTIFIER_NOT_FOUND,
            properties=properties,
            user_properties=user_properties,
        )
        buffer = bytearray()
        packet.encode(buffer)

        leftover_data, packet2 = decode_packet(memoryview(buffer))
        assert packet2 == packet
        assert not leftover_data

    def test_partial(self) -> None:
        packet = MQTTPublishCompletePacket(
            packet_id=65534, reason_code=ReasonCode.SUCCESS
        )
        buffer = bytearray()
        buffer2 = bytearray()
        encode_fixed_integer(packet.packet_id, buffer2, 2)

        packet.encode_fixed_header(0, buffer2, buffer)
        leftover_data, packet2 = decode_packet(memoryview(buffer))
        assert packet2 == packet
        assert not leftover_data

    def test_bad_reason_codes(self) -> None:
        for reason_code in ReasonCode.__members__.values():
            if reason_code not in MQTTPublishCompletePacket.allowed_reason_codes:
                with pytest.raises(ValueError):
                    MQTTPublishCompletePacket(reason_code=reason_code, packet_id=1)


class TestMQTTSubscribePacket:
    def test_minimal(self) -> None:
        subscriptions = [
            Subscription(
                pattern="foo",
                max_qos=QoS.AT_MOST_ONCE,
                no_local=False,
                retain_as_published=True,
                retain_handling=RetainHandling.SEND_RETAINED,
            )
        ]
        packet = MQTTSubscribePacket(packet_id=1, subscriptions=subscriptions)
        buffer = bytearray()
        packet.encode(buffer)

        leftover_data, packet2 = decode_packet(memoryview(buffer))
        assert packet2 == packet
        assert not leftover_data

    def test_full(self) -> None:
        subscriptions = [
            Subscription(
                pattern="foo",
                max_qos=QoS.AT_MOST_ONCE,
                no_local=False,
                retain_as_published=True,
                retain_handling=RetainHandling.SEND_RETAINED,
            ),
            Subscription(
                pattern="test/+/foo/#",
                max_qos=QoS.EXACTLY_ONCE,
                no_local=True,
                retain_as_published=False,
                retain_handling=RetainHandling.NO_RETAINED,
            ),
        ]
        properties: dict[PropertyType, PropertyValue] = {
            PropertyType.SUBSCRIPTION_IDENTIFIER: [268_435_455],
        }
        user_properties = {"foo": "bar", "key2": "value2"}
        packet = MQTTSubscribePacket(
            packet_id=65535,
            subscriptions=subscriptions,
            properties=properties,
            user_properties=user_properties,
        )
        buffer = bytearray()
        packet.encode(buffer)

        leftover_data, packet2 = decode_packet(memoryview(buffer))
        assert packet2 == packet
        assert not leftover_data


class TestMQTTSubscribeAckPacket:
    def test_minimal(self) -> None:
        packet = MQTTSubscribeAckPacket(packet_id=1, reason_codes=[ReasonCode.SUCCESS])
        buffer = bytearray()
        packet.encode(buffer)

        leftover_data, packet2 = decode_packet(memoryview(buffer))
        assert packet2 == packet
        assert not leftover_data

    def test_full(self) -> None:
        properties: dict[PropertyType, PropertyValue] = {
            PropertyType.REASON_STRING: "Bad data",
        }
        user_properties = {"foo": "bar", "key2": "value2"}
        packet = MQTTSubscribeAckPacket(
            packet_id=65535,
            reason_codes=[ReasonCode.UNSPECIFIED_ERROR, ReasonCode.GRANTED_QOS_1],
            properties=properties,
            user_properties=user_properties,
        )
        buffer = bytearray()
        packet.encode(buffer)

        leftover_data, packet2 = decode_packet(memoryview(buffer))
        assert packet2 == packet
        assert not leftover_data

    def test_bad_reason_codes(self) -> None:
        for reason_code in ReasonCode.__members__.values():
            if reason_code not in MQTTSubscribeAckPacket.allowed_reason_codes:
                with pytest.raises(ValueError):
                    MQTTSubscribeAckPacket(reason_codes=[reason_code], packet_id=1)


class TestMQTTUnsubscribePacket:
    def test_minimal(self) -> None:
        packet = MQTTUnsubscribePacket(packet_id=1, patterns=["foo"])
        buffer = bytearray()
        packet.encode(buffer)

        leftover_data, packet2 = decode_packet(memoryview(buffer))
        assert packet2 == packet
        assert not leftover_data

    def test_full(self) -> None:
        user_properties = {"foo": "bar", "key2": "value2"}
        packet = MQTTUnsubscribePacket(
            packet_id=65535,
            patterns=["foo", "another/topic/+"],
            user_properties=user_properties,
        )
        buffer = bytearray()
        packet.encode(buffer)

        leftover_data, packet2 = decode_packet(memoryview(buffer))
        assert packet2 == packet
        assert not leftover_data


class TestMQTTUnsubscribeAckPacket:
    def test_minimal(self) -> None:
        packet = MQTTUnsubscribeAckPacket(
            packet_id=1, reason_codes=[ReasonCode.SUCCESS]
        )
        buffer = bytearray()
        packet.encode(buffer)

        leftover_data, packet2 = decode_packet(memoryview(buffer))
        assert packet2 == packet
        assert not leftover_data

    def test_full(self) -> None:
        properties: dict[PropertyType, PropertyValue] = {
            PropertyType.REASON_STRING: "reason cöde"
        }
        user_properties = {"foo": "bar", "key2": "value2"}
        packet = MQTTUnsubscribeAckPacket(
            packet_id=65535,
            reason_codes=[ReasonCode.SUCCESS, ReasonCode.UNSPECIFIED_ERROR],
            properties=properties,
            user_properties=user_properties,
        )
        buffer = bytearray()
        packet.encode(buffer)

        leftover_data, packet2 = decode_packet(memoryview(buffer))
        assert packet2 == packet
        assert not leftover_data

    def test_bad_reason_codes(self) -> None:
        for reason_code in ReasonCode.__members__.values():
            if reason_code not in MQTTUnsubscribeAckPacket.allowed_reason_codes:
                with pytest.raises(ValueError):
                    MQTTUnsubscribeAckPacket(reason_codes=[reason_code], packet_id=1)


class TestMQTTPingRequestPacket:
    def test_minimal(self) -> None:
        packet = MQTTPingRequestPacket()
        buffer = bytearray()
        packet.encode(buffer)

        leftover_data, packet2 = decode_packet(memoryview(buffer))
        assert packet2 == packet
        assert not leftover_data


class TestMQTTPingResponsePacket:
    def test_minimal(self) -> None:
        packet = MQTTPingResponsePacket()
        buffer = bytearray()
        packet.encode(buffer)

        leftover_data, packet2 = decode_packet(memoryview(buffer))
        assert packet2 == packet
        assert not leftover_data


class TestMQTTDisconnectPacket:
    def test_minimal(self) -> None:
        packet = MQTTDisconnectPacket(reason_code=ReasonCode.SUCCESS)
        buffer = bytearray()
        packet.encode(buffer)

        leftover_data, packet2 = decode_packet(memoryview(buffer))
        assert packet2 == packet
        assert not leftover_data

    def test_full(self) -> None:
        properties: dict[PropertyType, PropertyValue] = {
            PropertyType.SESSION_EXPIRY_INTERVAL: 15,
            PropertyType.REASON_STRING: "reason cöde",
            PropertyType.SERVER_REFERENCE: "another server",
        }
        user_properties = {"foo": "bar", "key2": "value2"}
        packet = MQTTDisconnectPacket(
            reason_code=ReasonCode.UNSPECIFIED_ERROR,
            properties=properties,
            user_properties=user_properties,
        )
        buffer = bytearray()
        packet.encode(buffer)

        leftover_data, packet2 = decode_packet(memoryview(buffer))
        assert packet2 == packet
        assert not leftover_data

    def test_partial(self) -> None:
        packet = MQTTDisconnectPacket(reason_code=ReasonCode.NORMAL_DISCONNECTION)
        buffer = bytearray()

        packet.encode_fixed_header(0, b"", buffer)
        leftover_data, packet2 = decode_packet(memoryview(buffer))
        assert packet2 == packet
        assert not leftover_data

    def test_bad_reason_codes(self) -> None:
        for reason_code in ReasonCode.__members__.values():
            if reason_code not in MQTTDisconnectPacket.allowed_reason_codes:
                with pytest.raises(ValueError):
                    MQTTDisconnectPacket(reason_code=reason_code)


class TestMQTTAuthPacket:
    def test_minimal(self) -> None:
        packet = MQTTAuthPacket(reason_code=ReasonCode.SUCCESS)
        buffer = bytearray()
        packet.encode(buffer)

        leftover_data, packet2 = decode_packet(memoryview(buffer))
        assert packet2 == packet
        assert not leftover_data

    def test_full(self) -> None:
        properties: dict[PropertyType, PropertyValue] = {
            PropertyType.AUTHENTICATION_METHOD: "SCRAM-SHA-1",
            PropertyType.AUTHENTICATION_DATA: b"random",
            PropertyType.REASON_STRING: "reason",
        }
        user_properties = {"foo": "bar", "key2": "value2"}
        packet = MQTTAuthPacket(
            reason_code=ReasonCode.REAUTHENTICATE,
            properties=properties,
            user_properties=user_properties,
        )
        buffer = bytearray()
        packet.encode(buffer)

        leftover_data, packet2 = decode_packet(memoryview(buffer))
        assert packet2 == packet
        assert not leftover_data

    def test_bad_reason_codes(self) -> None:
        for reason_code in ReasonCode.__members__.values():
            if reason_code not in MQTTAuthPacket.allowed_reason_codes:
                with pytest.raises(ValueError):
                    MQTTAuthPacket(reason_code=reason_code)
