# Domain Context

## Run Request

A caller's immutable request to start one Antigravity execution. It includes
the prompt, workspace, execution policy, continuation identity, and optional
goal target identity. Preparing a Run Request validates and normalizes these
values and computes the deduplication identity.

## Run

A durable execution record created from a prepared Run Request. A Run owns a
stable `run_id`, persisted lifecycle state, logs, and one execution session.

## Execution Policy

The explicit controls that shape Antigravity execution: model, sandbox mode,
permission auto-approval, additional directories, timeout, and print versus
interactive mode.

## Execution Session

The live process container for a Run. Production uses a persistent tmux
session; tests can use an in-memory adapter through the same interface.

## Goal

A durable parent record that groups named Run targets and supplies inherited
execution policy plus a parallelism limit.

## Conversation

Antigravity's durable model-side context identity. Continuation always uses an
exact conversation ID; separate conversations do not share native context.
