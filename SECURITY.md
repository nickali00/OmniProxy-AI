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

During Phase 1 public preview, only the latest commit on `main` is supported.

## Deployment warning

The dashboard binds to loopback by default. Do not expose it directly to the
public Internet. Remote access requires, at minimum, TLS, administrative
authentication, rate limits and a reviewed reverse-proxy configuration.

Provider volumes isolate sessions but do not encrypt them at rest. Use host
disk encryption and protect Docker daemon access.
