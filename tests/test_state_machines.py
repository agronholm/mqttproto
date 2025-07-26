from __future__ import annotations

import pytest

from mqttproto import (
    MQTTClientState,
    MQTTConnAckPacket,
    MQTTConnectPacket,
    MQTTPublishAckPacket,
    MQTTPublishCompletePacket,
    MQTTPublishPacket,
    MQTTPublishReceivePacket,
    MQTTPublishReleasePacket,
    Pattern,
    PropertyType,
    QoS,
    ReasonCode,
    Subscription,
)
from mqttproto.broker_state_machine import (
    MQTTBrokerClientStateMachine,
    MQTTBrokerStateMachine,
)
from mqttproto.client_state_machine import MQTTClientStateMachine


@pytest.fixture
def client_session_pairs() -> (
    list[tuple[MQTTClientStateMachine, MQTTBrokerClientStateMachine]]
):
    client1 = MQTTClientStateMachine(client_id="client-1")
    client2 = MQTTClientStateMachine(client_id="client-2")

    broker = MQTTBrokerStateMachine()
    client_session1 = MQTTBrokerClientStateMachine()
    client_session2 = MQTTBrokerClientStateMachine()

    # Connect the clients
    client_session_pairs = [(client1, client_session1), (client2, client_session2)]
    for client, client_session in client_session_pairs:
        # Have the client send the CONNECT
        client.connect()
        assert client.state is MQTTClientState.CONNECTING
        packets = client_session.feed_bytes(client.get_outbound_data())
        assert len(packets) == 1
        assert isinstance(packets[0], MQTTConnectPacket)
        assert client_session.state is MQTTClientState.CONNECTING

        # Have the client session send the CONNACK response
        client_session.acknowledge_connect(ReasonCode.SUCCESS, None, False)
        packets = client.feed_bytes(client_session.get_outbound_data())
        assert len(packets) == 1
        assert isinstance(packets[0], MQTTConnAckPacket)
        # https://github.com/python/mypy/issues/17096
        assert client.state is MQTTClientState.CONNECTED  # type: ignore[comparison-overlap]
        assert client_session.state is MQTTClientState.CONNECTED

        # Add the client session to the broker
        broker.add_client_session(client_session)
        assert broker.client_state_machines[client.client_id] is client_session

    return client_session_pairs


@pytest.fixture
def connected_client(
    client_session_pairs: list[
        tuple[MQTTClientStateMachine, MQTTBrokerClientStateMachine]
    ],
) -> MQTTClientStateMachine:
    return client_session_pairs[0][0]


def test_subscribe_unsubscribe(connected_client: MQTTClientStateMachine) -> None:
    subscriptions = [Subscription(Pattern("foo/bar")), Subscription(Pattern("foo/baz"))]
    assert connected_client.subscribe(subscriptions) == 1

    assert connected_client.unsubscribe([Pattern("foo/bar")]) == 2
    assert connected_client.unsubscribe([Pattern("foo/baz")]) == 3


def test_client_publish_qos0(
    client_session_pairs: list[
        tuple[MQTTClientStateMachine, MQTTBrokerClientStateMachine]
    ],
) -> None:
    """Test the client publishing a QoS 0 message."""
    client1, client_session1 = client_session_pairs[0]
    client1.publish("test-topic", "payload")
    packets = client_session1.feed_bytes(client1.get_outbound_data())
    assert len(packets) == 1
    packet = packets[0]
    assert isinstance(packet, MQTTPublishPacket)
    assert packet.topic == "test-topic"
    assert packet.payload == "payload"


@pytest.mark.parametrize("qos", [QoS.AT_MOST_ONCE, QoS.AT_LEAST_ONCE, QoS.EXACTLY_ONCE])
def test_client_limit_qos(qos: QoS) -> None:
    """Test that a limited QoS is processed in the client."""
    client = MQTTClientStateMachine(client_id="client-X")
    client.connect()
    assert client.state is MQTTClientState.CONNECTING

    packet = MQTTConnAckPacket(
        reason_code=ReasonCode.SUCCESS,
        session_present=False,
        properties={PropertyType.MAXIMUM_QOS: qos} if qos < QoS.EXACTLY_ONCE else {},
    )
    buffer = bytearray()
    packet.encode(buffer)
    client.feed_bytes(buffer)
    assert client.maximum_qos == qos


def test_client_receive_qos0(
    client_session_pairs: list[
        tuple[MQTTClientStateMachine, MQTTBrokerClientStateMachine]
    ],
) -> None:
    """Test the client reciving a QoS 0 message from the broker."""
    client1, client_session1 = client_session_pairs[0]

    # Subscribe to test-topic
    packet_id = client1.subscribe([Subscription(Pattern("test-topic"))])
    assert packet_id == 1
    client_session1.feed_bytes(client1.get_outbound_data())
    client_session1.acknowledge_subscribe(packet_id, [ReasonCode.SUCCESS])
    client1.feed_bytes(client_session1.get_outbound_data())

    # Have the broker send a PUBLISH from "otherclient" to the client
    publish = MQTTPublishPacket(topic="test-topic", payload="payload")
    client_session1.deliver_publish(publish.topic, publish.payload)
    packets = client1.feed_bytes(client_session1.get_outbound_data())
    assert len(packets) == 1
    packet = packets[0]
    assert isinstance(packet, MQTTPublishPacket)
    assert packet.topic == "test-topic"
    assert packet.payload == "payload"
    assert not client1.get_outbound_data()


def test_publish_qos1(
    client_session_pairs: list[
        tuple[MQTTClientStateMachine, MQTTBrokerClientStateMachine]
    ],
) -> None:
    """Test the client publishing a QoS 1 message."""
    # Send the PUBLISH from the client
    client1, client_session1 = client_session_pairs[0]
    client1.publish("test-topic", "payload", qos=QoS.AT_LEAST_ONCE)
    packets = client_session1.feed_bytes(client1.get_outbound_data())
    assert len(packets) == 1
    packet = packets[0]
    assert isinstance(packet, MQTTPublishPacket)
    assert packet.topic == "test-topic"
    assert packet.payload == "payload"
    assert packet.qos is QoS.AT_LEAST_ONCE
    assert packet.packet_id == 1

    # Send the PUBACK from the client session
    client_session1.acknowledge_publish(packet.packet_id, ReasonCode.SUCCESS)
    packets = client1.feed_bytes(client_session1.get_outbound_data())
    assert len(packets) == 1
    packet = packets[0]
    assert isinstance(packet, MQTTPublishAckPacket)
    assert packet.reason_code is ReasonCode.SUCCESS


def test_client_receive_qos1(
    client_session_pairs: list[
        tuple[MQTTClientStateMachine, MQTTBrokerClientStateMachine]
    ],
) -> None:
    """Test the client reciving a QoS 0 message from the broker."""
    client1, client_session1 = client_session_pairs[0]

    # Subscribe to test-topic
    packet_id = client1.subscribe([Subscription(Pattern("test-topic"))])
    assert packet_id == 1
    client_session1.feed_bytes(client1.get_outbound_data())
    client_session1.acknowledge_subscribe(packet_id, [ReasonCode.SUCCESS])
    client1.feed_bytes(client_session1.get_outbound_data())

    # Have the broker send a PUBLISH from "otherclient" to the client
    publish = MQTTPublishPacket(
        topic="test-topic",
        payload="payload",
        qos=QoS.AT_LEAST_ONCE,
        packet_id=2,
    )
    client_session1.deliver_publish(publish.topic, publish.payload, qos=publish.qos)
    packets = client1.feed_bytes(client_session1.get_outbound_data())
    assert len(packets) == 1
    packet = packets[0]
    assert isinstance(packet, MQTTPublishPacket)
    assert packet.topic == "test-topic"
    assert packet.payload == "payload"

    # The client should have automatically responded with a PUBACK
    packets = client_session1.feed_bytes(client1.get_outbound_data())
    assert len(packets) == 1
    packet = packets[0]
    assert isinstance(packet, MQTTPublishAckPacket)
    assert packet.reason_code is ReasonCode.SUCCESS


def test_publish_qos2(
    client_session_pairs: list[
        tuple[MQTTClientStateMachine, MQTTBrokerClientStateMachine]
    ],
) -> None:
    """Test the client publishing a QoS 2 message."""
    # Send the PUBLISH from the client
    client1, client_session1 = client_session_pairs[0]
    client1.publish("test-topic", "payload", qos=QoS.EXACTLY_ONCE)
    packets = client_session1.feed_bytes(client1.get_outbound_data())
    assert len(packets) == 1
    packet = packets[0]
    assert isinstance(packet, MQTTPublishPacket)
    assert packet.topic == "test-topic"
    assert packet.payload == "payload"
    assert packet.qos is QoS.EXACTLY_ONCE
    assert packet.packet_id == 1

    # Send the PUBREC from the client session
    client_session1.acknowledge_publish(packet.packet_id, ReasonCode.SUCCESS)
    packets = client1.feed_bytes(client_session1.get_outbound_data())
    assert len(packets) == 1
    packet = packets[0]
    assert isinstance(packet, MQTTPublishReceivePacket)
    assert packet.reason_code is ReasonCode.SUCCESS
    assert packet.packet_id == 1

    # The client should have automatically responded with a PUBREL
    packets = client_session1.feed_bytes(client1.get_outbound_data())
    assert len(packets) == 1
    packet = packets[0]
    assert isinstance(packet, MQTTPublishReleasePacket)
    assert packet.reason_code is ReasonCode.SUCCESS
    assert packet.packet_id == 1

    # Send the PUBCOMP from the client session
    #
    packets = client1.feed_bytes(client_session1.get_outbound_data())
    assert len(packets) == 1
    packet = packets[0]
    assert isinstance(packet, MQTTPublishCompletePacket)
    assert packet.reason_code is ReasonCode.SUCCESS
    assert packet.packet_id == 1


def test_client_receive_qos2(
    client_session_pairs: list[
        tuple[MQTTClientStateMachine, MQTTBrokerClientStateMachine]
    ],
) -> None:
    """Test the client reciving a QoS 0 message from the broker."""
    client1, client_session1 = client_session_pairs[0]

    # Subscribe to test-topic
    packet_id = client1.subscribe([Subscription(Pattern("test-topic"))])
    assert packet_id == 1
    client_session1.feed_bytes(client1.get_outbound_data())
    client_session1.acknowledge_subscribe(packet_id, [ReasonCode.SUCCESS])
    client1.feed_bytes(client_session1.get_outbound_data())

    # Have the broker send a PUBLISH from "otherclient" to the client
    publish = MQTTPublishPacket(
        topic="test-topic",
        payload="payload",
        qos=QoS.EXACTLY_ONCE,
        packet_id=2,
    )
    client_session1.deliver_publish(publish.topic, publish.payload, qos=publish.qos)
    packets = client1.feed_bytes(client_session1.get_outbound_data())
    assert len(packets) == 1
    packet = packets[0]
    assert isinstance(packet, MQTTPublishPacket)
    assert packet.topic == "test-topic"
    assert packet.payload == "payload"

    # The client should have automatically responded with a PUBREC
    packets = client_session1.feed_bytes(client1.get_outbound_data())
    assert len(packets) == 1
    packet = packets[0]
    assert isinstance(packet, MQTTPublishReceivePacket)
    assert packet.reason_code is ReasonCode.SUCCESS

    # Have the broker respond with PUBREL
    client_session1.release_qos2_publish(packet.packet_id, ReasonCode.SUCCESS)
    packets = client1.feed_bytes(client_session1.get_outbound_data())
    assert len(packets) == 1
    packet = packets[0]
    assert isinstance(packet, MQTTPublishReleasePacket)

    # The client should have automatically responded with a PUBCOMP
    packets = client_session1.feed_bytes(client1.get_outbound_data())
    assert len(packets) == 1
    packet = packets[0]
    assert isinstance(packet, MQTTPublishCompletePacket)
    assert packet.reason_code is ReasonCode.SUCCESS


@pytest.mark.parametrize("retain", [None, False, True])
def test_client_retain(retain: bool | None) -> None:
    """Test that server's RETAINability is processed in the client"""
    client = MQTTClientStateMachine(client_id="client-X")
    client.connect()
    assert client.state is MQTTClientState.CONNECTING
    _bytes = client.get_outbound_data()  # ignored

    packet = MQTTConnAckPacket(
        reason_code=ReasonCode.SUCCESS,
        session_present=False,
        properties={PropertyType.RETAIN_AVAILABLE: retain}
        if retain is not None
        else {},
    )
    buffer = bytearray()
    packet.encode(buffer)
    client.feed_bytes(buffer)
    assert client.may_retain == (retain is not False)
