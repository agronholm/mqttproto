.. image:: https://github.com/agronholm/mqttproto/actions/workflows/test.yml/badge.svg
  :target: https://github.com/agronholm/mqttproto/actions/workflows/test.yml
  :alt: Build Status
.. image:: https://coveralls.io/repos/github/agronholm/mqttproto/badge.svg?branch=main
  :target: https://coveralls.io/github/agronholm/mqttproto?branch=main
  :alt: Code Coverage
.. image:: https://readthedocs.org/projects/mqttproto/badge/?version=latest
  :target: https://mqttproto.readthedocs.io/en/latest/?badge=latest
  :alt: Documentation

This library contains a sans-io_ implementation of the MQTT_ v5 protocol.

Contents:

* State machines appropriate for implementing MQTT_ clients and brokers
* Asynchronous client and broker implementations, compatible with both Trio_ and
  asyncio_, via the AnyIO_ library
* Synchronous client implementation, implemented by using an asyncio_ event loop thread
  behind the scenes

While the provided client I/O implementations are intended for production use, the
broker implementation should only be used in very lightweight scenarios where high
performance or a broad feature set are not required. In those cases, broker
implementations from vendors like EMQX_ or HiveMQ_ are recommended instead.

The state machine implementation tries to adhere to the MQTT v5 protocol as tightly as
possible, and this project has certain automatic safeguards to ensure correctness:

* Ruff_ linting
* Passes Mypy_ in strict mode
* Documentation built with "fail-on-warning" enabled

You can find the documentation `here <https://mqttproto.readthedocs.io/>`_.

.. _sans-io: https://sans-io.readthedocs.io/
.. _MQTT: https://docs.oasis-open.org/mqtt/mqtt/v5.0/mqtt-v5.0.html
.. _Trio: https://github.com/python-trio/trio
.. _asyncio: https://docs.python.org/3/library/asyncio.html
.. _AnyIO: https://pypi.org/project/anyio/
.. _EMQX: https://www.emqx.com/en
.. _HiveMQ: https://www.hivemq.com/
.. _Ruff: https://docs.astral.sh/ruff/
.. _Mypy: https://mypy-lang.org/
.. _Pyright: https://github.com/microsoft/pyright
