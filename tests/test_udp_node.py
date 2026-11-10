"""Tests of the built-in UDP Message node (``behavior_tree_widget.network_nodes``).

Requirement (feature request "UDP message node"): a node that sends a UDP message to a
given address, with the fields

* Destination IP (a string),
* Destination Port (an integer),
* Message (a string).
"""

from __future__ import annotations

import socket
import threading

import pytest
from PySide6.QtWidgets import QLineEdit, QSpinBox

from behavior_tree_widget import NodeStatus, UdpMessageNodeWidget, network_nodes
from behavior_tree_widget.network_nodes import UDP_MESSAGE, resolve_udp_address

from conftest import fast_config, run_until_idle

IP, PORT, MESSAGE = "Destination IP", "Destination Port", "Message"


# ============================================================================ helpers
@pytest.fixture
def receiver():
    """A UDP socket bound to an ephemeral port of 127.0.0.1."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(2.0)
    yield sock
    sock.close()


def udp_node(bt, host: str = "127.0.0.1", port: int = 5005, message: str = "hello") -> UdpMessageNodeWidget:
    """A UDP Message node connected below the Root."""
    node = bt.AddNode(UDP_MESSAGE, 0, 220)
    node.SetField(IP, host)
    node.SetField(PORT, port)
    node.SetField(MESSAGE, message)
    bt.Connect(bt.GetRootNode(), node)
    return node


def run(qtbot, bt) -> list[str]:
    """Execute the tree once and return what executionFinished reported."""
    fast_config(bt)
    finished = []
    bt.executionFinished.connect(finished.append)
    bt.Execute()
    run_until_idle(qtbot, bt)
    return finished


def assert_fails(qtbot, bt, node, *fragments: str) -> None:
    errors = []
    bt.nodeError.connect(lambda failed, message: errors.append((failed, message)))
    assert run(qtbot, bt) == ["Failed"]
    assert node.GetStatus() is NodeStatus.FAILED
    for fragment in fragments:
        assert fragment in node.GetError()
    assert errors and errors[0][0] is node


def no_datagram(sock: socket.socket, wait: float = 0.3) -> bool:
    sock.settimeout(wait)
    try:
        sock.recvfrom(65535)
    except TimeoutError:
        return True
    return False


# ============================================================================ the node type
def test_udp_message_is_a_built_in_node_type(bt):
    names = bt.GetNodeTypes()
    assert names[names.index("Stop") + 1] == UDP_MESSAGE
    assert bt.GetNodeType(UDP_MESSAGE) is UdpMessageNodeWidget
    assert UdpMessageNodeWidget.TYPE_NAME == UDP_MESSAGE == "UDP Message"


def test_udp_message_has_its_own_menu_group(bt):
    entries = bt._node_menu_entries()
    groups = [group for group, _, _ in entries]
    assert [(name, label) for group, name, label in entries if group == "network"] == [(UDP_MESSAGE, "UDP Message")]
    assert groups.index("execution") < groups.index("network") < groups.index("leaf")


def test_the_fields_and_their_editors(bt):
    node = bt.AddNode(UDP_MESSAGE, 0, 220)
    assert node.GetFields() == {IP: "127.0.0.1", PORT: 5005, MESSAGE: ""}
    assert list(node.GetFields()) == [IP, PORT, MESSAGE]
    assert isinstance(node.field_editor(IP), QLineEdit)
    assert isinstance(node.field_editor(PORT), QSpinBox)
    assert isinstance(node.field_editor(MESSAGE), QLineEdit)
    assert node.GetTitle() == "UDP Message"


def test_the_node_runs_on_a_worker_thread():
    assert UdpMessageNodeWidget.RUN_IN_THREAD  # a name lookup must not block the window


def test_fields_edited_in_the_node_are_used(qtbot, bt, receiver):
    node = bt.AddNode(UDP_MESSAGE, 0, 220)
    bt.Connect(bt.GetRootNode(), node)
    node.field_editor(IP).setText("127.0.0.1")
    node.field_editor(PORT).setValue(receiver.getsockname()[1])
    node.field_editor(MESSAGE).setText("typed")
    assert run(qtbot, bt) == ["Succeeded"]
    assert receiver.recvfrom(65535)[0] == b"typed"


# ============================================================================ sending
def test_the_message_is_sent_as_one_utf8_datagram(qtbot, bt, receiver):
    message = "status: ok é ✓"
    node = udp_node(bt, port=receiver.getsockname()[1], message=message)
    assert run(qtbot, bt) == ["Succeeded"]
    assert node.GetStatus() is NodeStatus.SUCCEEDED
    data, _ = receiver.recvfrom(65535)
    assert data == message.encode("utf-8")
    assert no_datagram(receiver)  # exactly one


def test_an_empty_message_sends_an_empty_datagram(qtbot, bt, receiver):
    udp_node(bt, port=receiver.getsockname()[1], message="")
    assert run(qtbot, bt) == ["Succeeded"]
    assert receiver.recvfrom(65535)[0] == b""


def test_spaces_around_the_address_are_ignored(qtbot, bt, receiver):
    udp_node(bt, host="  127.0.0.1 ", port=receiver.getsockname()[1])
    assert run(qtbot, bt) == ["Succeeded"]
    assert receiver.recvfrom(65535)[0] == b"hello"


def test_a_host_name_can_be_used(qtbot, bt, receiver):
    udp_node(bt, host="localhost", port=receiver.getsockname()[1])
    assert run(qtbot, bt) == ["Succeeded"]
    assert receiver.recvfrom(65535)[0] == b"hello"


def test_ipv6_addresses_can_be_used(qtbot, bt):
    if not socket.has_ipv6:
        pytest.skip("no IPv6 support")
    sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    try:
        try:
            sock.bind(("::1", 0))
        except OSError:
            pytest.skip("no IPv6 loopback")
        sock.settimeout(2.0)
        udp_node(bt, host="::1", port=sock.getsockname()[1], message="six")
        assert run(qtbot, bt) == ["Succeeded"]
        assert sock.recvfrom(65535)[0] == b"six"
    finally:
        sock.close()


def test_every_execution_sends_again(qtbot, bt, receiver):
    udp_node(bt, port=receiver.getsockname()[1], message="again")
    for _ in range(3):
        assert run(qtbot, bt)[-1] == "Succeeded"
    assert [receiver.recvfrom(65535)[0] for _ in range(3)] == [b"again"] * 3


def test_broadcast_is_allowed(qtbot, bt, monkeypatch):
    sent = []

    class FakeSocket:
        def __init__(self, family, kind, protocol=0):
            self.family, self.options = family, {}
            sent.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def settimeout(self, timeout):
            self.timeout = timeout

        def setsockopt(self, level, option, value):
            self.options[(level, option)] = value

        def sendto(self, data, address):
            self.data, self.address = data, address
            return len(data)

    monkeypatch.setattr(network_nodes.socket, "socket", FakeSocket)
    udp_node(bt, host="255.255.255.255", port=9999, message="everyone")
    assert run(qtbot, bt) == ["Succeeded"]
    assert len(sent) == 1
    assert sent[0].options[(socket.SOL_SOCKET, socket.SO_BROADCAST)] == 1
    assert sent[0].address == ("255.255.255.255", 9999) and sent[0].data == b"everyone"


# ============================================================================ failures
@pytest.mark.parametrize("port", [0, -1, 65536, 2**31 - 1])
def test_a_port_outside_1_to_65535_fails(qtbot, bt, port):
    node = udp_node(bt, port=port)
    assert_fails(qtbot, bt, node, "Destination Port must be between 1 and 65535", str(port))


@pytest.mark.parametrize("host", ["", "   "])
def test_an_empty_address_fails(qtbot, bt, host):
    node = udp_node(bt, host=host)
    assert_fails(qtbot, bt, node, "Destination IP is empty")


def test_an_address_that_cannot_be_resolved_fails(qtbot, bt, monkeypatch):
    def fail(*args, **kwargs):
        raise socket.gaierror(11001, "getaddrinfo failed")

    monkeypatch.setattr(network_nodes.socket, "getaddrinfo", fail)
    node = udp_node(bt, host="no.such.host.invalid")
    assert_fails(qtbot, bt, node, "OSError", "'no.such.host.invalid' is not a valid address")


def test_an_invalid_ip_address_fails(qtbot, bt):
    node = udp_node(bt, host="bad..address")  # rejected before any name lookup
    assert_fails(qtbot, bt, node, "is not a valid address")


def test_a_message_too_long_for_a_datagram_fails(qtbot, bt, receiver):
    node = udp_node(bt, port=receiver.getsockname()[1], message="x" * 70000)
    assert_fails(qtbot, bt, node, "OnRun raised")
    assert no_datagram(receiver)


@pytest.mark.parametrize(
    "key, value, expected",
    [(IP, 127, "Destination IP must be a string"), (PORT, "80", "Destination Port must be an integer"),
     (PORT, True, "Destination Port must be an integer"), (MESSAGE, b"raw", "Message must be a string")],
)
def test_fields_of_the_wrong_type_fail(qtbot, bt, key, value, expected):
    node = udp_node(bt)
    node.SetField(key, value)
    assert_fails(qtbot, bt, node, "TypeError", expected)


def test_a_stop_during_the_name_lookup_sends_nothing(qtbot, bt, receiver, monkeypatch):
    looking_up, release = threading.Event(), threading.Event()
    real_resolve = network_nodes.resolve_udp_address

    def slow_resolve(host, port):
        looking_up.set()
        release.wait(5)
        return real_resolve(host, port)

    monkeypatch.setattr(network_nodes, "resolve_udp_address", slow_resolve)
    node = udp_node(bt, port=receiver.getsockname()[1])
    fast_config(bt)
    bt.Execute()
    qtbot.waitUntil(looking_up.is_set, timeout=2000)
    assert node.GetStatus() is NodeStatus.RUNNING
    bt.Stop()
    release.set()
    qtbot.waitUntil(lambda: node._worker_future is not None and node._worker_future.done(), timeout=2000)
    assert node._worker_future.result() is False
    assert no_datagram(receiver)
    assert node.GetStatus() is NodeStatus.READY


# ============================================================================ address resolution
def test_resolution_prefers_ipv4_for_host_names(monkeypatch):
    infos = [
        (socket.AF_INET6, socket.SOCK_DGRAM, 17, "", ("::1", 7, 0, 0)),
        (socket.AF_INET, socket.SOCK_DGRAM, 17, "", ("127.0.0.1", 7)),
    ]
    monkeypatch.setattr(network_nodes.socket, "getaddrinfo", lambda *args, **kwargs: list(infos))
    assert resolve_udp_address("localhost", 7) == (socket.AF_INET, 17, ("127.0.0.1", 7))


def test_resolution_keeps_ipv6_when_there_is_no_ipv4(monkeypatch):
    infos = [(socket.AF_INET6, socket.SOCK_DGRAM, 17, "", ("::1", 7, 0, 0))]
    monkeypatch.setattr(network_nodes.socket, "getaddrinfo", lambda *args, **kwargs: list(infos))
    assert resolve_udp_address("ip6-localhost", 7) == (socket.AF_INET6, 17, ("::1", 7, 0, 0))


def test_resolution_of_literal_addresses():
    assert resolve_udp_address("127.0.0.1", 5005)[2] == ("127.0.0.1", 5005)
    assert resolve_udp_address("127.0.0.1", 5005)[0] == socket.AF_INET


# ============================================================================ files
def test_udp_message_nodes_are_saved_and_loaded(bt, tmp_path):
    node = udp_node(bt, host="10.0.0.7", port=9000, message="saved ✓")
    path = tmp_path / "udp.json"
    assert bt.SaveTree(str(path))
    assert bt.LoadTree(str(path))
    (loaded,) = bt.GetRootNode().GetChildren()
    assert type(loaded) is UdpMessageNodeWidget and loaded.GetId() == node.GetId()
    assert loaded.GetFields() == {IP: "10.0.0.7", PORT: 9000, MESSAGE: "saved ✓"}


def test_udp_message_nodes_are_copied_and_pasted(bt):
    node = udp_node(bt, host="10.0.0.8", port=9001, message="copy")
    assert bt.CopyNodes([node])
    (copy,) = bt.PasteNodes()
    assert type(copy) is UdpMessageNodeWidget
    assert copy.GetFields() == node.GetFields()
