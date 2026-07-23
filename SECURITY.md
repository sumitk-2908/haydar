# Security Policy

## Supported versions

Haydar is pre-1.0. Security fixes are applied to the latest released version
only. Always run the most recent release.

| Version | Supported |
|---------|-----------|
| latest  | ✅ |
| older   | ❌ |

## Threat model

Haydar is a **fully-local, single-user** tool. The person running it owns the
machine, the index, and the files being indexed. There is no server, no network
service, and no multi-tenant boundary — for any locally-sourced content, the
attacker and the victim are the same person.

Consequences:

- "Path traversal" / "markup injection" over your *own* indexed files are
  robustness/correctness concerns, not privilege-crossing vulnerabilities.
- Snippets in the UI are rendered as plain text, and file content is never
  executed.

**The one trust boundary that matters is the ripgrep download.** The keyword
search binary is fetched from GitHub Releases and **executed**, so it is verified
against a hardcoded SHA-256 checksum (`src/haydar/ripgrep.py`), fail-closed: an
unknown or mismatched checksum is a hard error, never a warning. The pinned
checksums must be updated in lockstep with the ripgrep `VERSION`. Never bypass
this verification.

Distribution note: end users run a frozen PyInstaller EXE, so their dependency
versions are fixed at build time. The EXEs are currently **unsigned**, so Windows
SmartScreen warns on first launch — verify the published `.sha256` (see
`verify.ps1`) to confirm integrity.

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security reports.

Instead, report privately via GitHub's
[private vulnerability reporting](https://github.com/haydar-search/haydar/security/advisories/new)
(Security → Report a vulnerability). Include:

- affected version and platform,
- a description and, ideally, a minimal reproduction,
- the impact you observed.

We aim to acknowledge reports within a few days and will coordinate a fix and
disclosure timeline with you.
