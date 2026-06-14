# Security Policy

## Reporting

Do not open a public issue for a vulnerability involving command execution,
credential exposure, path traversal, or unintended access to Antigravity
conversation data.

Report vulnerabilities through GitHub's private vulnerability reporting for
this repository. Include reproduction steps, affected versions, impact, and
any suggested mitigation.

## Security model

This bridge is not a sandbox. Antigravity runs with the current user's OS
permissions and may execute commands, modify files, and access the network.
The `workspace` parameter provides context, not isolation.

The bridge does not intentionally read or transmit Antigravity OAuth
credentials. It invokes the locally installed CLI and reads local conversation
metadata and trajectory files.
