"""Prepare a private HTTPS profile; does not deploy, provision users or alter .env."""

import argparse
import ipaddress
import json
import os
import re
from pathlib import Path

from app.config_bundles import write_private

CADDY_IMAGE = "caddy:2.11.9-alpine@sha256:ae5e525ca6773b0a4ce579c774cb39b3147e1576084139229517bdbcfe3ed812"


def validate_host(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        if len(value) > 253 or not re.fullmatch(r"(?=.{1,253}$)[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", value) or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in value.split(".")):
            raise ValueError("Invalid HTTPS hostname") from None
        return value.lower()
    if address.version != 4 or address.is_unspecified or address.is_multicast:
        raise ValueError("Use a specific IPv4 address or DNS hostname")
    return value


def configuration(host, bind_address, https_port=443, http_port=8181, tls_subnet="10.253.180.0/28", operator_uid=None, operator_gid=None, docker_gid=0):
    operator_uid = os.getuid() if operator_uid is None else operator_uid
    operator_gid = os.getgid() if operator_gid is None else operator_gid
    if any(not isinstance(value, int) or value < 0 for value in (operator_uid, operator_gid, docker_gid)):
        raise ValueError("Use valid numeric service identities")
    host = validate_host(host)
    address = ipaddress.ip_address(bind_address)
    if address.version != 4 or not address.is_private or address.is_unspecified:
        raise ValueError("Bind the local profile to a specific private IPv4 address")
    if not (1 <= https_port <= 65535 and 1 <= http_port <= 65535) or https_port == http_port:
        raise ValueError("Use two distinct valid ports")
    subnet = ipaddress.ip_network(tls_subnet)
    if subnet.version != 4 or not subnet.is_private or subnet.is_loopback or not 28 <= subnet.prefixlen <= 30:
        raise ValueError("Use a dedicated private IPv4 /28 to /30 subnet")
    authority = host + (":" + str(https_port) if https_port != 443 else "")
    caddy = """{
    admin off
    auto_https disable_redirects
    https_port 8443
    http_port 8080
    skip_install_trust
    default_sni HOST
    pki {
        ca local {
            intermediate_lifetime 2192d
        }
    }
    servers {
        protocols h1 h2
        max_header_size 16KB
        timeouts {
            read_header 10s
            read_body 35s
            idle 2m
        }
    }
    log default {
        level ERROR
    }
}
https://HOST:8443 {
    tls {
        protocols tls1.2 tls1.3
        issuer internal {
            lifetime 1826d
        }
    }
    header {
        Strict-Transport-Security "max-age=31536000"
        X-Content-Type-Options nosniff
        Referrer-Policy no-referrer
        -Server
    }
    request_body {
        max_size 32MB
    }
    reverse_proxy unix//run/gateway/gateway.sock {
        header_up -Forwarded
        header_up -X-Forwarded-Host
        header_up X-Forwarded-Proto https
        header_up X-Forwarded-For {remote_host}
        flush_interval -1
    }
}
http://HOST:8080 {
    @browse {
        method GET HEAD
        not path /v1/* /v1beta/* /api/* /admin/v1/*
    }
    handle @browse {
        redir https://AUTHORITY{uri} 308
    }
    handle {
        respond "Use the HTTPS endpoint." 426
    }
}
""".replace("HOST", host).replace("AUTHORITY", authority)
    # Paths are relative to the base compose file, not to this overlay's directory.
    overlay = f"""# Generated local security profile; retained by ordinary docker compose commands.
services:
  gateway:
    ports: !reset []
    environment:
      DASHBOARD_AUTH_REQUIRED: "true"
      REQUIRE_HTTPS: "true"
      GATEWAY_UDS: /run/gateway/gateway.sock
      MAINTENANCE_TOKEN_PATH: /run/control/maintenance.token
      DASHBOARD_ALLOWED_HOSTS: {json.dumps(host + ',localhost,127.0.0.1,gateway')}
    volumes:
      - security_ingress:/run/gateway
      - security_control:/run/control:ro
    mem_limit: 2g
    pids_limit: 128
    healthcheck:
      test: ["CMD", "python", "-m", "scripts.healthcheck"]
  maintenance-runner:
    user: "{operator_uid}:{operator_gid}"
    group_add: ["{docker_gid}", "10001"]
    environment:
      MAINTENANCE_TOKEN_PATH: /run/control/maintenance.token
    volumes:
      - security_control:/run/control:ro
    pids_limit: 256
  tls-proxy:
    image: {CADDY_IMAGE}
    user: "10001:10001"
    restart: unless-stopped
    init: true
    read_only: true
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    pids_limit: 128
    mem_limit: 256m
    ports:
      - "{bind_address}:{https_port}:8443"
      - "{bind_address}:{http_port}:8080"
    volumes:
      - ./.security/Caddyfile:/etc/caddy/Caddyfile:ro
      - security_ingress:/run/gateway:ro
      - security_tls:/data
    tmpfs:
      - /config:rw,noexec,nosuid,size=8m,uid=10001,gid=10001
      - /tmp:rw,noexec,nosuid,size=8m,uid=10001,gid=10001
    depends_on:
      gateway:
        condition: service_healthy
    networks:
      - tls_link
    logging:
      driver: json-file
      options:
        max-size: "5m"
        max-file: "2"
networks:
  tls_link:
    # Only the TLS ingress joins this bridge; internal networks cannot publish ports.
    ipam:
      config:
        - subnet: {subnet}
volumes:
  security_ingress:
  security_control:
  security_tls:
"""
    return caddy, overlay


def prepare(directory, host, bind_address, https_port=443, http_port=8181, tls_subnet="10.253.180.0/28", docker_gid=None):
    if docker_gid is None:
        docker_gid = os.stat("/var/run/docker.sock").st_gid
    caddy, overlay = configuration(host, bind_address, https_port, http_port, tls_subnet, docker_gid=docker_gid)
    directory = Path(directory)
    os.mkdir(directory, 0o700)
    # The mounted config has no secrets and is readable by the non-root proxy.
    with (directory / "Caddyfile").open("x") as output:
        output.write(caddy)
    os.chmod(directory / "Caddyfile", 0o644)
    write_private(directory / "https.override.yml", overlay)
    write_private(directory / "manifest.json", json.dumps({"https_origin": "https://" + host + (":" + str(https_port) if https_port != 443 else ""), "local_ca_requires_explicit_client_trust": True, "gateway_transport": "private_unix_socket", "caddy_image": CADDY_IMAGE}, indent=2) + "\n")
    return directory.resolve()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--bind-address", required=True)
    parser.add_argument("--https-port", type=int, default=443)
    parser.add_argument("--http-port", type=int, default=8181)
    parser.add_argument("--tls-subnet", default="10.253.180.0/28")
    parser.add_argument("--docker-gid", type=int)
    args = parser.parse_args()
    print(json.dumps({"prepared_directory": str(prepare(args.directory, args.host, args.bind_address, args.https_port, args.http_port, args.tls_subnet, args.docker_gid))}))
