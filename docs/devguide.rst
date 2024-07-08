Developing new I/O implementations
==================================

.. py:currentmodule:: mqttproto

This project contains both the appropriate state machines implementing the MQTT v5
protocol, and also asynchronous and synchronous implementations of both the MQTT client
and broker. While the client implementation is intended for production use, the broker
should only be used in very lightweight scenarios where high performance or a broad
feature set are not requirements. In most cases, implementations from vendors like EMQX_
or HiveMQ_ are recommended instead.

.. _EMQX: https://www.emqx.com/
.. _HiveMQ: https://www.hivemq.com/

What the state machines handle for you
--------------------------------------

There are 3 state machines provided by this project:

* :class:`~.client_state_machine.MQTTClientStateMachine`: the MQTT client's state
  machine
* :class:`~.broker_state_machine.MQTTBrokerStateMachine`: the MQTT broker's state
  machine
* :class:`~.broker_state_machine.MQTTBrokerClientStateMachine`: the state machine
  representing an MQTT client session on the broker

The client and broker state machines handle as much of the MQTT protocol for you as
possible without getting in the way:

* Parsing inbound packets from bytes
* Encoding outbound packets into bytes
* Buffering bytes containing any incomplete packets
* Raising the appropriate exceptions when malformed packets or protocol violations are
  detected
* Transitioning to different states based on the received packets and the previous state
* Sending automatic replies to certain packets (e.g. ``PINGREQ``), when this does not
  need a decision from the I/O implementation (this is why you must make sure you always
  flush the output too after receiving a packet)
* Sending retained messages to clients when they subscribe to matching topics
* Keeping track of used packet IDs
* Keeping track of the client's active subscriptions
* Keeping track of pending subscribe, unsubscribe and QoS 1/2 publish requests
* Enforcing maximum message sizes
* Enforcing the receive maximum (max number of unacknowledged QoS 1/2 messages)

What the state machines will NOT handle for you
-----------------------------------------------

The state machines don't interact with the "real world", meaning:

* They **cannot** use any form of sockets or networking
* They will **not** deal with timing or clocks
* The broker state machine will **not** do any authentication or authorization

These restrictions also have the following practical consequences:

* They will **not** send periodic :class:`~.MQTTPingRequestPacket` packets to prevent
  keepalive-based disconnections
* They will **not** send :class:`~.Will` messages automatically, as this may require
  waiting for the delay to expire
* They will **not** disconnect clients due to expiring keepalive timers

The I/O implementations will need to provide all this functionality.

Responsibilities of client I/O implementations
----------------------------------------------

.. py:currentmodule:: mqttproto.client_state_machine

Responsibilities for client I/O implementations are:

* Continuously read data from the transport stream, and pass it to the state machine
  using its :meth:`~.MQTTClientStateMachine.feed_bytes` method
* After receiving *any* packet from the broker:

  * Fetch any outbound data to the transport stream using
    :meth:`~.MQTTClientStateMachine.get_outbound_data`, and send it to the transport
    stream. This is important, as the protocol might send automatic replies for certain
    messages.
  * Check the session state. If the session has transitioned to the ``DISCONNECTED``
    state, then close the transport stream.

Responsibilities of broker I/O implementations
----------------------------------------------

.. py:currentmodule:: mqttproto.broker_state_machine

Responsibilities for broker I/O implementations are:

* Continuously read data from the transport stream of each connected client, and pass it
  to the appropriate state machine using its
  :meth:`~.MQTTBrokerClientStateMachine.feed_bytes` method
* After receiving *any* packet from a client:

  * Fetch any outbound data to the transport stream using
    :meth:`~.MQTTBrokerClientStateMachine.get_outbound_data`, and send it to the
    transport stream. This is important, as the protocol might send automatic replies
    for certain messages.
  * Check the session state. If the session has transitioned to the ``DISCONNECTED``
    state, then close the transport stream.

* When receiving an :class:`~.MQTTConnectPacket`, perform whatever authentication checks
  necessary for your implementation, and then use

  * :class:`~.MQTTConnectPacket` (respond using
    :meth:`~.MQTTBrokerClientStateMachine.acknowledge_connect`)
  * :class:`~.MQTTSubscribePacket` (respond with
    :meth:`~.MQTTBrokerClientStateMachine.acknowledge_subscribe`)
* If a message is published to the broker, attempt delivery to all client sessions.
  The protocol state machine will ensure that only clients with matching subscriptions
  actually receive the message.
* If a client disconnects abrubtly (without sending a ``DISCONNECT`` message with code
  0x00, and it had a :class:`~.Will`, publish that will after its specified
  `delay interval`_, provided that the client does not resume its session before the
  delay expires.
* If a client has ``keepalive`` greater than 0, close the client's transport stream
  after the configured period of inactivity (and remember to send any :class:`~.Will`
  message that was requested by the client

.. _delay interval: https://docs.oasis-open.org/mqtt/mqtt/v5.0/os/mqtt-v5.0-os.html#_Will_Delay_Interval_1

Debugging
---------

The state machines log both inbound and outbound packets using the ``mqttproto`` logger,
on the :data:`~logging.DEBUG` level. :mod:`Configuring logging <logging.config>` for
this logger will enable you to see exactly what's being received and transmitted by the
state machines.

The concrete I/O implementations of client and broker use their respective loggers,
``mqttproto.client`` and ``mqttproto.broker``, respectively, also on the
:data:`~logging.DEBUG` level.
