"""pytest-xdist example for sanic server

Install testing tools:

    $ pip install pytest pytest-xdist

Run with xdist params:

    $ pytest examples/pytest_xdist.py -n 8  # 8 workers
"""
import re

import pytest

from sanic_testing import SanicTestClient
from sanic_testing.testing import PORT as PORT_BASE

from sanic import Sanic
from sanic.response import text


@pytest.fixture(scope="session")
def test_port(worker_id):
    num_id = m.group(0) if (m := re.search(r"[0-9]+", worker_id)) else 0
    return PORT_BASE + int(num_id)


@pytest.fixture(scope="session")
def app():
    app = Sanic("Example")

    @app.route("/")
    async def index(request):
        return text("OK")

    return app


@pytest.fixture(scope="session")
def client(app, test_port):
    return SanicTestClient(app, test_port)


@pytest.mark.parametrize("run_id", range(100))
def test_index(client, run_id):
    request, response = client._sanic_endpoint_test("get", "/")
    assert response.status == 200
    assert response.text == "OK"
