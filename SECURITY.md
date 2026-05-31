# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in bluei, please report it privately.
Do not open a public issue.

**Email:** <security@bluei.dev>

Include:

- A clear description of the vulnerability
- Steps to reproduce
- Affected versions (if known)
- Any potential mitigations you've identified

## Scope

bluei scans repositories for issues including hardcoded secrets. Security vulnerabilities
in bluei itself could expose:

- Repositories being scanned
- GitHub credentials or tokens used by bluei
- Findings data stored in bluei's state directory

## Supported Versions

| Version | Supported |
|---------|-----------|
| 2.x     | Yes       |
| < 2.0   | No        |

## Disclosure Timeline

1. You report the vulnerability privately
2. We acknowledge receipt within 48 hours
3. We validate and develop a fix
4. We release a patch and publish a security advisory
5. Credit is given to the reporter (unless you prefer to remain anonymous)

## Hall of Fame

We appreciate responsible disclosure. Reporters who follow this policy will be acknowledged
in the security advisory for the vulnerability they reported.
