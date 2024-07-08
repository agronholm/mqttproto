Contributing to mqttproto
=========================

.. highlight:: bash

If you wish to contribute a fix or feature to ``mqttproto``, please follow the following
guidelines.

When you make a pull request against the main ``mqttproto`` codebase, Github runs the
test suite against your modified code. Before making a pull request, you should ensure
that the modified code passes tests and code quality checks locally.

Running the test suite
----------------------

The test suite requires a real MQTT broker running on port 1883 on the local machine.
To make this easy for the developer, a `docker compose`_ configuration is provided.
To use it, you need Docker_ (or a suitable replacement). On Linux, unless you're using
Docker Desktop, you may need to also install the compose (v2) plugin (named
``docker-compose-plugin``, or similar) separately.

Once you have the necessary tools installed, you can start the services with this
command::

    docker compose up -d

You can run the test suite two ways: either with tox_, or by running pytest_ directly.

To run tox_ against all supported (of those present on your system) Python versions::

    tox

Tox will handle the installation of dependencies in separate virtual environments.

To pass arguments to the underlying pytest_ command, you can add them after ``--``, like
this::

    tox -- -k somekeyword

To use pytest directly, you can set up a virtual environment and install the project in
development mode along with its test dependencies:

.. tabs::

    .. code-tab:: bash Linux or macOS

        python -m venv venv
        source venv/bin/activate
        pip install -e .[test]

    .. code-tab:: winbatch Windows

        py -m venv venv
        venv\Scripts\activate
        pip install -e .[test]

Now you can just run pytest_::

    pytest

Building the documentation
--------------------------

To build the documentation, run ``tox -e docs``. This will place the documentation in
``build/sphinx/html`` where you can open ``index.html`` to view the formatted
documentation.

``mqttproto`` uses ReadTheDocs_ to automatically build the documentation so the above
procedure is only necessary if you are modifying the documentation and wish to check the
results before committing.

``mqttproto`` uses pre-commit_ to perform several code style/quality checks. It is
recommended to activate pre-commit_ on your local clone of the repository (using
``pre-commit install``) to ensure that your changes will pass the same checks on GitHub.

Making a pull request on Github
-------------------------------

To get your changes merged to the main codebase, you need a Github account.

#. Fork the repository (if you don't have your own fork of it yet) by navigating to the
   `main mqttproto repository`_ and clicking on "Fork" near the top right corner.
#. Clone the forked repository to your local machine with
   ``git clone git@github.com/yourusername/mqttproto``.
#. Create a branch for your pull request, like ``git checkout -b myfixname``
#. Make the desired changes to the code base.
#. Commit your changes locally. If your changes close an existing issue, add the text
   ``Fixes #XXX.`` or ``Closes #XXX.`` to the commit message (where XXX is the issue
   number).
#. Push the changeset(s) to your forked repository (``git push``)
#. Navigate to Pull requests page on the original repository (not your fork) and click
   "New pull request"
#. Click on the text "compare across forks".
#. Select your own fork as the head repository and then select the correct branch name.
#. Click on "Create pull request".

If you have trouble, consult the `pull request making guide`_ on opensource.com.

.. _Docker: https://docs.docker.com/desktop/#download-and-install
.. _docker compose: https://docs.docker.com/compose/
.. _tox: https://tox.readthedocs.io/en/latest/install.html
.. _pre-commit: https://pre-commit.com/#installation
.. _pytest: https://pypi.org/project/pytest/
.. _ReadTheDocs: https://readthedocs.org/
.. _main mqttproto repository: https://github.com/agronholm/mqttproto
.. _pull request making guide: https://opensource.com/article/19/7/create-pull-request-github
