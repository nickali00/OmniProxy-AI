"""Run with private file permissions and an optional proxy-only Unix socket."""

import os

import uvicorn


def main():
    os.umask(0o077)
    socket = os.environ.get("GATEWAY_UDS", "")
    options = {"uds": socket, "proxy_headers": True, "forwarded_allow_ips": "*"} if socket else {"host": "0.0.0.0", "port": 8000, "proxy_headers": False}
    # The wildcard applies only to the private Unix socket, never a TCP listener.
    uvicorn.run("app.main:app", server_header=False, access_log=False, **options)


if __name__ == "__main__":
    main()
