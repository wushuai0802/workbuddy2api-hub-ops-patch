"""Deny real upstream networking in child test processes; allow loopback only."""
import os
import sys

if os.environ.get("WB_OPS_OFFLINE") == "1":
    def audit(event, arguments):
        if event == "socket.connect":
            address = arguments[1]
            if isinstance(address, tuple) and str(address[0]).lower() not in (
                    "127.0.0.1", "::1", "localhost"):
                raise OSError("offline test: non-loopback connection denied")
        elif event == "socket.getaddrinfo":
            host = arguments[0]
            if host is not None and str(host).lower() not in (
                    "127.0.0.1", "::1", "localhost", "0.0.0.0", "::"):
                raise OSError("offline test: non-loopback DNS denied")

    sys.addaudithook(audit)
