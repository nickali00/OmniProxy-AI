# Versioning and support policy

Contract major (`1`) is independent of product SemVer (`1.0.0-rc.4`).
Candidate releases are for evaluation: no stable1.0 support window begins
before a dated stable release and its tested migration/compatibility report.

Within a stable contract major, additive optional response fields and new
opt-in endpoints/capabilities are permitted. Removing fields/endpoints,
changing defaults with effects, weakening ownership or changing required
request shapes requires a new contract major and migration guide. Provider
or CLI compatibility changes need a capability-matrix entry and reproducible
tests even when the public request schema does not change. Security fixes
may disable an unsafe combination immediately and must state the reason.

Before stable1.0, a breaking candidate change requires a new rc version,
changelog, regenerated schema, conformance report and explicit migration
notes. No stable1.0 compatibility guarantee is claimed for candidate builds.

For stable versions the project policy is to keep the current contract major
and its previous major available for at least180 days after the new major's
stable release. Deprecations must include an announcement date, replacement,
removal date and migration example in the changelog. This is a project
maintenance policy, not a commercial SLA or a claim of staffed24/7 support.
The rc has no scheduled removals. End-of-support dates must be published
before stable release, not inferred from an rc number.

Release checks: full tests, regenerated OpenAPI check, standalone HTTP/SDK
conformance, real coding client, image smoke, migration/backup/restore,
capability matrix and known exclusions. Archive reports with exact versions.
Database downgrade is not implied by image rollback: restore a matching
backup into a new destination first. PostgreSQL-to-SQLite reverse migration,
multi-host filesystem coordination and provider account recovery are outside
this candidate's tested upgrade contract.
