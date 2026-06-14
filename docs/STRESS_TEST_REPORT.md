# Stress Test Report

Date: 2026-06-14

## Scope

Two stress campaigns exercised orchestration, persistence, lifecycle
reconciliation, goals, janitor cleanup, transcripts, configuration, and
cross-process locking. The permanent regression suites are:

- `tests/test_stress_round1.py`: 17 scenarios
- `tests/test_stress_round2.py`: 10 scenarios

## Round 1

| ID | Scenario | Initial result | Final result |
|---|---|---|---|
| 01 | Global capacity under 40 concurrent distinct starts | Pass | Pass |
| 02 | Deduplication under 40 identical starts | Pass | Pass |
| 03 | Concurrent disk-store field updates | Pass | Pass |
| 04 | Concurrent terminal transitions and sentinel removal | Pass | Pass |
| 05 | Four concurrent goal-target registrations | Pass | Pass |
| 06 | Fifth goal target rejected | Pass | Pass |
| 07 | Unrelated runs do not consume goal capacity | Fail | Pass |
| 08 | Concurrent memory-store field updates | Fail | Pass |
| 09 | Goal lifecycle through memory-store adapter | Fail | Pass |
| 10 | Cancel race does not overwrite completion | Fail | Pass |
| 11 | Status race does not overwrite completion | Fail | Pass |
| 12 | Janitor race does not overwrite completion | Fail | Pass |
| 13 | Concurrent cancellation is idempotent | Pass | Pass |
| 14 | Concurrent janitors preserve valid state | Pass | Pass |
| 15 | Atomic JSON writes never expose partial JSON | Pass | Pass |
| 16 | Duplicate concurrent goal target has one winner | Pass | Pass |
| 17 | Active registry survives transition churn | Pass | Pass |

## Round 2

| ID | Scenario | Initial result | Final result |
|---|---|---|---|
| 18 | Configured parallelism cannot exceed product limit | Fail | Pass |
| 19 | Invalid parallelism has actionable error | Fail | Pass |
| 20 | Spawn failure releases capacity | Pass | Pass |
| 21 | Goal status survives missing target state | Fail | Pass |
| 22 | Janitor preserves malformed durable state | Fail | Pass |
| 23 | Two orchestrators share one global limit | Pass | Pass |
| 24 | Multi-process store updates are lossless | Pass | Pass |
| 25 | Transcript reads tolerate concurrent appends | Pass | Pass |
| 26 | Corrupt active sentinel does not hide valid runs | Pass | Pass |
| 27 | Conditional terminal state is monotonic | Pass | Pass |

## Defects Fixed

1. Goal capacity counted unrelated active runs.
2. `MemoryRunStore` did not honor the locking interface.
3. Goal creation bypassed the configured store adapter.
4. Cancellation could overwrite a concurrently completed run.
5. Status reconciliation could overwrite a concurrently completed run.
6. Janitor reconciliation could overwrite a concurrently completed run.
7. Environment configuration could exceed the product parallel limit of four.
8. Invalid parallel configuration emitted a low-level integer parse error.
9. Missing target state crashed the entire goal-status query.
10. Janitor deleted durable run evidence when `state.json` was malformed.

## Architecture Result

The store module now owns atomic run mutation and conditional active-state
transitions. This deepens the store interface: callers get transaction
semantics and terminal-state protection without reimplementing lock, load,
validate, save, and sentinel ordering. The disk and memory adapters now honor
the same interface.

Janitor cleanup is conservative around unclassifiable durable state. Goal
aggregation degrades a missing target into a failed target result instead of
failing the entire goal query. Global and per-goal capacity are enforced as
separate invariants.
