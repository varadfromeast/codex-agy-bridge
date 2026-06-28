# Agent Instructions

## Token Budget Hygiene

- Prefer targeted reads over full-file reads. Use `rg`, `sed -n`, `nl -ba`, and focused `rtk grep`/`rtk read` ranges before opening large files.
- For long reports or docs, extract the relevant section first, such as `Phase 2` or `Large Results`, instead of reading the whole document.
- During TDD, run the narrow failing test or module while iterating; run the full non-live suite once near the end unless a broad change makes earlier full verification necessary.
- Prefer `git diff --stat` and file-scoped diffs over broad full diffs while iterating.
- Use code search and targeted tests before reaching for heavier repository-wide analysis tools.
