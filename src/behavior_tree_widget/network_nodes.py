"""Built-in leaf node that sends a UDP message."""

from __future__ import annotations

import socket

from .nodes import LeafNodeWidget

__all__ = ["UDP_MESSAGE", "UdpMessageNodeWidget"]

UDP_MESSAGE = "UDP Message"
MIN_PORT = 1
MAX_PORT = 65535


def resolve_udp_address(host: str, port: int) -> tuple[int, int, tuple]:
    """``(family, protocol, address)`` to send UDP datagrams to ``host`` and ``port``.

    ``host`` is an IPv4 or IPv6 address or a host name. When a name has IPv4 and IPv6
    addresses (e.g. ``localhost``), the IPv4 address is used.

    Raises:
        OSError: ``host`` cannot be resolved.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
    except (socket.gaierror, UnicodeError) as error:  # UnicodeError: e.g. an empty label in a host name
        raise OSError(f"Destination IP {host!r} is not a valid address ({error})") from error
    if not infos:
        raise OSError(f"Destination IP {host!r} has no address")
    infos.sort(key=lambda info: info[0] != socket.AF_INET)  # IPv4 first, otherwise in the resolver's order
    family, _, protocol, _, address = infos[0]
    return family, protocol, address


class UdpMessageNodeWidget(LeafNodeWidget):
    """Sends ``Message`` as one UDP datagram to ``Destination IP`` and ``Destination Port``.

    The message is encoded as UTF-8. The node succeeds once the datagram was sent (UDP
    does not report whether it arrived) and fails when the address cannot be used, the
    port is not between 1 and 65535 or the system refuses to send it (e.g. a message too
    long for one datagram). ``Destination IP`` may be an IPv4 or IPv6 address or a host
    name; broadcast addresses are allowed. ``OnRun`` runs on a worker thread, so a slow
    name lookup does not block the window.
    """

    TYPE_NAME = UDP_MESSAGE
    _title = UDP_MESSAGE
    IP_FIELD = "Destination IP"
    PORT_FIELD = "Destination Port"
    MESSAGE_FIELD = "Message"
    _fields = {IP_FIELD: "127.0.0.1", PORT_FIELD: 5005, MESSAGE_FIELD: ""}
    SEND_TIMEOUT = 5.0  # seconds

    def _title_tooltip(self) -> str:
        return f"{UDP_MESSAGE} - sends Message to Destination IP and Destination Port over UDP"

    def OnRun(self, tree):
        host = self.GetField(self.IP_FIELD)
        port = self.GetField(self.PORT_FIELD)
        message = self.GetField(self.MESSAGE_FIELD)
        if not isinstance(host, str):
            raise TypeError(f"{self.IP_FIELD} must be a string, not {type(host).__name__}")
        if isinstance(port, bool) or not isinstance(port, int):
            raise TypeError(f"{self.PORT_FIELD} must be an integer, not {type(port).__name__}")
        if not isinstance(message, str):
            raise TypeError(f"{self.MESSAGE_FIELD} must be a string, not {type(message).__name__}")
        host = host.strip()
        if not host:
            raise ValueError(f"{self.IP_FIELD} is empty")
        if not MIN_PORT <= port <= MAX_PORT:
            raise ValueError(f"{self.PORT_FIELD} must be between {MIN_PORT} and {MAX_PORT}, not {port}")
        data = message.encode("utf-8")
        family, protocol, address = resolve_udp_address(host, port)
        if self.CancelRequested():
            return False  # stopped while the name was looked up: send nothing
        with socket.socket(family, socket.SOCK_DGRAM, protocol) as sock:
            sock.settimeout(self.SEND_TIMEOUT)
            if family == socket.AF_INET:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sent = sock.sendto(data, address)
        if sent != len(data):
            raise OSError(f"only {sent} of {len(data)} bytes were sent")
        return True
