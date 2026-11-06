#!/bin/sh
set -eu
# The deployment preparer generates URL-safe passwords. No secret in argv.
case "${OMNI_APP_PASSWORD:-}" in
  ""|*[!A-Za-z0-9_-]*) exit 1 ;;
esac
psql --username "$POSTGRES_USER" --dbname postgres --set ON_ERROR_STOP=1 <<SQL
CREATE USER omniproxy_runtime WITH PASSWORD '$OMNI_APP_PASSWORD' NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE DATABASE omniproxy OWNER omniproxy_runtime;
REVOKE ALL ON DATABASE omniproxy FROM PUBLIC;
GRANT CONNECT ON DATABASE omniproxy TO omniproxy_runtime;
SQL
