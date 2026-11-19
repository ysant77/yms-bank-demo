"""Tests open no network connection outside this machine.

``tests/conftest.py`` turns the guard on for every test of the session. A connection, a datagram
or a name lookup for a non-loopback address is refused with an error before any packet is sent.
Loopback (127.0.0.0/8, ::1, "localhost") and local (AF_UNIX) sockets stay allowed.

Limit: the guard is in the test process only. A subprocess that a test starts (git, a second
Python) is not covered by it.
"""

import socket

import pytest

REFUSED = "non-loopback network access is refused in tests"


def test_a_connection_to_another_machine_is_refused():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(2)  # without the guard the test fails in 2 s, not after a long wait
        with pytest.raises(RuntimeError, match=REFUSED):
            s.connect(("192.0.2.1", 80))  # TEST-NET-1: never a real host


def test_connect_ex_does_not_hide_the_refusal():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(2)  # without the guard the test fails in 2 s, not after a long wait
        with pytest.raises(RuntimeError, match=REFUSED):
            s.connect_ex(("192.0.2.1", 80))


def test_a_datagram_to_another_machine_is_refused():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        with pytest.raises(RuntimeError, match=REFUSED):
            s.sendto(b"x", ("192.0.2.1", 53))


def test_a_name_lookup_of_another_host_is_refused():
    with pytest.raises(RuntimeError, match=REFUSED):
        socket.create_connection(("example.invalid", 443), timeout=1)
    with pytest.raises(RuntimeError, match=REFUSED):
        socket.getaddrinfo("pypi.org", 443)


def test_a_loopback_connection_is_allowed():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.create_connection(server.getsockname(), timeout=5) as client:
            peer, _ = server.accept()
            with peer:
                client.sendall(b"ok")
                assert peer.recv(2) == b"ok"
    assert socket.getaddrinfo("localhost", 80)


def test_the_guard_decides_by_address():
    from conftest import is_loopback

    allowed = ["127.0.0.1", "127.8.9.10", "::1", "localhost", "LOCALHOST", None, ""]
    refused = ["192.0.2.1", "10.0.0.1", "0.0.0.0", "::", "2001:db8::1", "example.invalid"]
    for host in allowed:
        assert is_loopback(host), host
    for host in refused:
        assert not is_loopback(host), host
