"""Probe the gateway through its private Unix socket when configured."""

import http.client
import os
import socket
import urllib.request


def main():
    endpoint = os.environ.get("GATEWAY_UDS", "")
    if not endpoint:
        with urllib.request.urlopen("http://127.0.0.1:8000/healthz", timeout=3) as response:
            if response.status != 200:
                raise SystemExit(1)
        return
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(3)
        connection.connect(endpoint)
        connection.sendall(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        response = http.client.HTTPResponse(connection)
        response.begin()
        if response.status != 200:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
