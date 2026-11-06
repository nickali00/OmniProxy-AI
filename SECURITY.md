# Security policy

OmniProxy AI handles local API keys and authenticated provider sessions.
Please do not publish suspected vulnerabilities, credentials, OAuth codes or
private logs in a public issue.

## Reporting a vulnerability

Use the repository's private
[security advisory form](https://github.com/nickali00/OmniProxy-AI/security/advisories/new).
Include:

- the affected version or commit;
- the smallest reproducible example;
- expected and observed behavior;
- the potential impact;
- suggested remediation, if available.

Remove API keys, account identifiers, prompts and provider tokens from every
attachment. Acknowledgement and remediation timelines depend on severity and
maintainer availability.

## Supported version

The current candidate is 1.0.0-rc.3. Stable support and deprecation policy is
defined in [spec/versioning.md](spec/versioning.md); candidate status does
not imply an independent security certification.

## Deployment warning

The dashboard binds to loopback by default. Do not expose it directly to the
public Internet. Remote access requires, at minimum, TLS, administrative
authentication, rate limits and a reviewed reverse-proxy configuration.

Use the [local HTTPS profile](docs/wiki/sicurezza-https.md) for LAN access.
It removes the gateway TCP port, enables an operator login without adopting
legacy keys into tenants, and verifies TLS at clients using the instance CA.
HTTP API requests are refused. Import the public CA only through a trusted
channel; never bypass certificate verification. The host, Docker operators,
and private same-host service links remain trusted boundaries.

Provider volumes isolate sessions but do not encrypt them at rest. Use host
disk encryption and protect Docker daemon access.
