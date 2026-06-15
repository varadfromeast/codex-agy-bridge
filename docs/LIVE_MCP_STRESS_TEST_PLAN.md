# Remaining Live MCP Stress Tests

## Scope

Only three interactive-input behaviors remain unverified:

| Test | Result |
| --- | --- |
| L49 - Send text without Enter | PASS |
| L50 - Special characters and multiline text | PASS |
| L53 - Burst interactive input | PASS |

All previously recorded defects are fixed and live verified. Latest automated
verification: `131 passed`; Ruff clean.

## Rules

- Use a fresh stdio MCP server with an isolated temporary state/workspace.
- Start one interactive Run and wait for `session_state=awaiting_input`.
- Use unique nonce strings and verify behavior through `agy_transcript`.
- Record only bounded transcript excerpts.
- Cancel the Run and confirm no test-owned tmux session remains.
- If a case fails, use TDD: one failing regression test, minimal fix, unit
  verification, then live rerun.

## L49 - Send Text Without Enter

1. Send `BUFFERED_<nonce>` with `enter=false`.
2. Poll transcript for five seconds.
3. Assert no response contains the nonce.
4. Send an empty string with `enter=true`.
5. Poll until a completed response echoes `BUFFERED_<nonce>`.
6. Poll again and assert the nonce was consumed exactly once.

Pass: no response before Enter; exactly one response after Enter.

Likely fix area if failed: `terminal.send_text` / tmux `send-keys` submission.

Result: `PASS`

## L50 - Special Characters And Multiline Text

Send one prompt containing a unique nonce plus:

```text
$HOME ; `command` "quotes" 'single quotes' \ backslash
line two
line three
```

Ask the interactive Run to return the input inside a JSON string. Verify the
transcript contains the nonce and every literal segment exactly once, with no
shell expansion, truncation, or line merging.

Pass: exact literal content is returned.

Likely fix area if failed: `terminal.send_text` argument handling.

Result: `PASS`

## L53 - Burst Interactive Input

1. Rapidly send five prompts: `BURST_<nonce>_0` through `BURST_<nonce>_4`.
2. Ask the Run to echo each prompt exactly.
3. Poll transcript until all five responses are complete.
4. Assert every nonce appears exactly once and in numeric order.

Pass: no prompt is dropped, merged, duplicated, or reordered.

Likely fix area if failed: serialize interactive sends per Run before calling
tmux `send-keys`.

Result: `PASS`

## Defect Repair Loop

For each failed live case:

1. Add one public-interface regression test reproducing the failure.
2. Run it and confirm RED.
3. Run GitNexus impact analysis with `summaryOnly=true` before editing.
4. Implement the smallest fix.
5. Confirm GREEN, run the related test module, then run the full suite and
   Ruff.
6. Rerun the failed live case and update its result.

## Completion

The remaining stress plan is complete when:

- L49, L50, and L53 are `PASS`.
- No new defect remains open.
- Full tests and Ruff pass.
- Active test Runs and test-owned tmux sessions equal zero.

Completion evidence:

- L49: no transcript event before Enter; exactly one buffered prompt after
  Enter.
- L50: literal shell-sensitive characters survived and Antigravity returned
  the three multiline segments separately.
- L53: five rapidly submitted prompts appeared as ordered, distinct
  `USER_INPUT` events and were processed one at a time.
- All live test Runs were canceled and no test-owned tmux session remained.
