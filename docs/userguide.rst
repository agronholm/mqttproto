User guide
==========

Installing
----------

.. highlight:: bash

To install the library without any extras::

    pip install mqttproto

If you need websockets connectivity, you will want to include the ``websockets`` extra::

    pip install mqttproto[websockets]

Using the async client
----------------------

.. py:currentmodule:: mqttproto.async_client
.. highlight:: python

The asynchronous client needs to be used as an asynchronous context manager to manage
its life cycle, as per the principles of `structured concurrency`_. Exiting the context
manager will clean up any resources used, like terminating active connections any active
subscriptions and cancelling pending publish operations.

.. _structured concurrency: https://en.wikipedia.org/wiki/Structured_concurrency

Connecting to the broker
++++++++++++++++++++++++

.. tabs::

   .. tab:: MQTT/TCP

      This connects to ``localhost:1883``:

      .. literalinclude:: snippets/userguide/async_tcp.py

   .. tab:: MQTT/UNIX

      This connects to a UNIX domain socket at ``/path/to/broker.sock``:

      .. literalinclude:: snippets/userguide/async_unix.py

   .. tab:: Websockets/TCP

      This connects to ``http://localhost/ws``:

      .. literalinclude:: snippets/userguide/async_websockets.py

When you need to use TLS_ (formerly SSL) for securing your connections, you can simply
pass ``ssl=True`` to :class:`~.AsyncMQTTClient`, or you can provide your own
:class:`~ssl.SSLContext` if you need to provide a client certificate or add a custom
root certificate.

.. note:: Enabling TLS changes the default ports as follows:

    * raw MQTT: default port changes to 8883
    * websockets: default port changes to 443

.. tabs::

   .. tab:: MQTT/TCP

      This connects to ``localhost:8883``:

      .. literalinclude:: snippets/userguide/async_tcp_tls.py

   .. tab:: MQTT/UNIX

      This connects to a UNIX domain socket at ``/path/to/broker.sock``:

      .. literalinclude:: snippets/userguide/async_unix_tls.py

   .. tab:: Websockets/TCP

      This connects to ``https://localhost/ws``:

      .. literalinclude:: snippets/userguide/async_websockets_tls.py

.. _TLS: https://en.wikipedia.org/wiki/Transport_Layer_Security

Subscribing to topics
+++++++++++++++++++++

Subscribing is also done via context managers. Here's an example of how to subscribe to
the ``my/sample/topic`` topic:

.. literalinclude:: snippets/userguide/async_subscribe_single.py

You can subscribe to multiple topics or topic patterns at once:

.. literalinclude:: snippets/userguide/async_subscribe_multi.py

Often, you will find yourself needing to subscribe from several tasks at once. This is
fine too:

.. literalinclude:: snippets/userguide/async_subscribe_multitask.py

.. tip:: If you find yourself needing so many nested context managers that this actually
    hurts code readability, try using :class:`~contextlib.AsyncExitStack` to manage the
    context managers.

.. seealso:: :meth:`~.AsyncMQTTClient.subscribe`

Publishing messages
+++++++++++++++++++

To publish a message to the broker, call the :meth:`~.AsyncMQTTClient.publish` method:

.. literalinclude:: snippets/userguide/async_publish.py

Using the sync client
---------------------

.. py:currentmodule:: mqttproto.sync_client

The synchronous client (:class:`~.MQTTClient`) is a wrapper for the asynchronous client,
allowing users to not care about the details of the asynchronous implementation.
Otherwise, the same principles apply to the synchronous client.

Connecting to the broker:

.. tabs::

   .. tab:: MQTT/TCP

      This connects to ``localhost:1883``:

      .. literalinclude:: snippets/userguide/sync_tcp.py

   .. tab:: MQTT/UNIX

      This connects to a UNIX domain socket at ``/path/to/broker.sock``:

      .. literalinclude:: snippets/userguide/sync_unix.py

   .. tab:: Websockets/TCP

      This connects to ``http://localhost/ws``:

      .. literalinclude:: snippets/userguide/sync_websockets.py

Connecting with TLS:

.. tabs::

   .. tab:: MQTT/TCP

      This connects to ``localhost:8883``:

      .. literalinclude:: snippets/userguide/sync_tcp_tls.py

   .. tab:: MQTT/UNIX

      This connects to a UNIX domain socket at ``/path/to/broker.sock``:

      .. literalinclude:: snippets/userguide/sync_unix_tls.py

   .. tab:: Websockets/TCP

      This connects to ``https://localhost/ws``:

      .. literalinclude:: snippets/userguide/sync_websockets_tls.py
