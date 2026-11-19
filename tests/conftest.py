import importlib.util
import ipaddress
import os
import socket
import threading
from datetime import UTC, datetime

import pytest

from bank import ledger

# Locally the fraud tests skip without the optional extra; in CI a skip would hide them.
if os.environ.get("CI") and importlib.util.find_spec("sklearn") is None:
    raise RuntimeError("CI must install the fraud extra (uv sync --extra fraud)")


# Tests open no network connection outside this machine (a test that reaches the network can leak
# data or depend on a service). Loopback and AF_UNIX stay allowed. The guard covers this process
# only, not a subprocess that a test starts. This file is a trust path: a change here can turn the
# guard off for every test.
NETWORK_REFUSED = "non-loopback network access is refused in tests"


def is_loopback(host: object) -> bool:
    """True for a loopback address, the name "localhost", or no host (a local lookup)."""
    if host is None or host == "" or host == b"":
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _check(address: object) -> None:
    if isinstance(address, tuple) and address and not is_loopback(address[0]):
        raise RuntimeError(f"{NETWORK_REFUSED}: {address[0]!r}")


@pytest.fixture(autouse=True, scope="session")
def refuse_network():
    real = {
        "connect": socket.socket.connect,
        "connect_ex": socket.socket.connect_ex,
        "sendto": socket.socket.sendto,
        "getaddrinfo": socket.getaddrinfo,
    }

    def connect(self, address):
        _check(address)
        return real["connect"](self, address)

    def connect_ex(self, address):
        _check(address)
        return real["connect_ex"](self, address)

    def sendto(self, data, *args):
        _check(args[-1])
        return real["sendto"](self, data, *args)

    def getaddrinfo(host, *args, **kwargs):
        if not is_loopback(host):
            raise RuntimeError(f"{NETWORK_REFUSED}: {host!r}")
        return real["getaddrinfo"](host, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.socket.sendto = sendto
    socket.getaddrinfo = getaddrinfo
    try:
        yield
    finally:
        socket.socket.connect = real["connect"]
        socket.socket.connect_ex = real["connect_ex"]
        socket.socket.sendto = real["sendto"]
        socket.getaddrinfo = real["getaddrinfo"]


@pytest.fixture
def conn(tmp_path):
    connection = ledger.open_db(tmp_path / "bank.db")
    yield connection
    connection.close()


@pytest.fixture
def at():
    """Build a UTC timestamp for a posting date."""

    def make(day: str, time: str = "12:00") -> datetime:
        return datetime.fromisoformat(f"{day}T{time}").replace(tzinfo=UTC)

    return make


@pytest.fixture
def run_together():
    """Run ``work(start)`` in ``n`` threads; each calls ``start()`` to wait for the others.

    A thread that fails breaks the barrier, so the other threads stop instead of waiting
    forever, and a thread that does not finish in time is reported. Returns the errors.
    """

    def run(n: int, work, timeout: float = 60.0) -> list[str]:
        barrier = threading.Barrier(n, timeout=timeout)
        errors: list[str] = []
        lock = threading.Lock()

        def worker():
            try:
                work(barrier.wait)
            except BaseException as exc:  # noqa: BLE001 - returned to the test
                barrier.abort()
                with lock:
                    errors.append(repr(exc))

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(n)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout)
        errors.extend("thread did not finish" for thread in threads if thread.is_alive())
        return errors

    return run


@pytest.fixture(scope="session")
def fraud_run():
    """The fraud score trained once per test session (synthetic data, fixed seed)."""
    from fraud import model

    return model.run()
