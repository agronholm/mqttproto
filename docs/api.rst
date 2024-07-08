API Reference
==============

Enumerated types
----------------

.. autoenum:: mqttproto.PropertyType
.. autoenum:: mqttproto.QoS
.. autoenum:: mqttproto.ReasonCode
.. autoenum:: mqttproto.RetainHandling
.. autoenum:: mqttproto.MQTTClientState

Supporting classes
------------------

.. autoclass:: mqttproto.Subscription
.. autoclass:: mqttproto.Will

MQTT packet classes
-------------------

.. autoclass:: mqttproto.MQTTPacket
.. autoclass:: mqttproto.MQTTConnectPacket
.. autoclass:: mqttproto.MQTTConnAckPacket
.. autoclass:: mqttproto.MQTTPublishPacket
.. autoclass:: mqttproto.MQTTPublishAckPacket
.. autoclass:: mqttproto.MQTTPublishReceivePacket
.. autoclass:: mqttproto.MQTTPublishReleasePacket
.. autoclass:: mqttproto.MQTTPublishCompletePacket
.. autoclass:: mqttproto.MQTTSubscribePacket
.. autoclass:: mqttproto.MQTTSubscribeAckPacket
.. autoclass:: mqttproto.MQTTUnsubscribePacket
.. autoclass:: mqttproto.MQTTUnsubscribeAckPacket
.. autoclass:: mqttproto.MQTTPingRequestPacket
.. autoclass:: mqttproto.MQTTPingResponsePacket
.. autoclass:: mqttproto.MQTTAuthPacket
.. autoclass:: mqttproto.MQTTDisconnectPacket

Client-side state machine
-------------------------

.. autoclass:: mqttproto.client_state_machine.MQTTClientStateMachine
   :inherited-members:

Broker-side state machines
--------------------------

.. autoclass:: mqttproto.broker_state_machine.MQTTBrokerStateMachine
.. autoclass:: mqttproto.broker_state_machine.MQTTBrokerClientStateMachine
   :inherited-members:

Concrete client implementations
-------------------------------

.. autoclass:: mqttproto.async_client.AsyncMQTTClient
.. autoclass:: mqttproto.async_client.AsyncMQTTSubscription
.. autoclass:: mqttproto.sync_client.MQTTClient
.. autoclass:: mqttproto.sync_client.MQTTSubscription

Concrete broker implementation
------------------------------

.. autoclass:: mqttproto.async_broker.AsyncMQTTBroker
.. autoclass:: mqttproto.async_broker.MQTTAuthenticator
.. autoclass:: mqttproto.async_broker.MQTTAuthorizer
