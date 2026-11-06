# Changelog

## 1.0.0-rc.3 — 2026-11-06

Publishes the implemented roadmap, public contract, conformance suite and
minimized verification reports. Adds a dedicated external-CSS login card
and gateway status derived from the effective request URL, including custom
ports. HTTPS client migration verified existing keys, aliases, limits and
disabled states without regenerating credentials. Private host instructions,
configuration, CA material, backups and separate checkouts are excluded.

Local operator CLI can explicitly extend an active operator session by
1–5 calendar years while preserving the token and revocation behavior.
Casa degli Agenti reminders now support dashboard expiry metadata without
receiving the dashboard secret, independently of certificate reminders.

Adds a verified local HTTPS profile with Caddy, a private Unix gateway socket,
authenticated dashboard access that preserves personal API keys, secure
cookies, authentication throttling and authenticated maintenance control.
Public upstreams require HTTPS in the secure profile; remote PostgreSQL
requires verified TLS. Existing SQLite/WAL permissions are restricted to 0600.
Pillow 13.0.0 and Cryptography 50.0.2 replace vulnerable versions; image codecs
are restricted before decoding. Broker bases and Docker CLI are updated,
unused package managers removed from runtime images. Gateway/provider errors
and proxy handling are hardened. Media network restrictions are regression
tested, with their existing protocol/format allowlists preserved.

Deployment requires provisioning an operator and installing the dedicated
public CA on clients. Update clients to the HTTPS URL; API keys and model
aliases are preserved. HTTP API calls receive 426 instead of running in
cleartext. The local override is retained by normal Compose updates.
System-library and vendor-binary advisories that lack an available packaged
fix remain documented in the security report; no zero-vulnerability or full
host audit claim is made. See docs/wiki/sicurezza-https.md.

## 1.0.0-rc.2 — 2026-11-06

Adds named, collapsible groups to the API-key dashboard: create, rename,
move keys, ungroup and remove a container. Membership and collapsed state
persist in SQLite/PostgreSQL. Grouping changes only dashboard organization;
credentials, model aliases, routing, limits and grants are preserved.
Removing a group keeps its keys valid and ungrouped. Existing profiles start
ungrouped. Core schema3 adds two metadata tables; back up before upgrading.
Public contract remains1. Roll back using rc.1 and a matching database backup.

## 1.0.0-rc.1 — 2026-11-05

Candidate for contract1 evaluation. Adds versioned discovery, public OpenAPI,
standalone HTTP conformance and three SDK integrations to the implemented
proxy, limits/budgets, multimodal files/media, managed MCP, approved repository
jobs, team identities, PostgreSQL, backup/migrations and OTLP.

Responses continuation now returns the actual previous_response_id in both
normal and streamed response objects. Stored history and authorization
semantics are unchanged. New job proposal hashes bind the base and revision;
existing stored proposals keep their original hash. Codex image+client tool
requests fail explicitly rather than dropping the image. Internal Codex
tools/search are disabled on both execution paths; client tools and
authorized managed MCP retain separate ownership.

Upgrade SQLite through additive component migrations (core2, identity1,
jobs3, citations1); make a consistent backup before replacing the gateway.
Team mode is opt-in and cannot silently downgrade after tenants exist.
Default personal SQLite deployment remains supported. PostgreSQL migration
requires an empty target and a reviewed source hash. Rollback requires the
previous image and a matching restored backup; image-only database downgrade
is not promised. See docs/wiki/operazioni.md.

Known exclusions and exact tested clients are in spec/compatibility.md.
This candidate is not stable1.0, independently adopted as a standard, or a
claim that all vendor functionality is supported. No deprecations scheduled.
