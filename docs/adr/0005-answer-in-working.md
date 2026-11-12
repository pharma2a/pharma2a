# 0005: The agent answers in `working` instead of waiting for the supplier

**Status:** Accepted

**Date:** 2026-11-02

## Context

With `returnImmediately: false`, the default, A2A 1.0 asks the server to wait for a terminal
or interrupted task state before answering `SendMessage`. An order reaches such a state only
when the supplier answers, hours or days later. No HTTP connection lasts that long.

## Options considered

1. Hold the connection until the supplier answers.
2. Require clients to send `returnImmediately: true`.
3. Answer as soon as the order has left, in `working`, and say so; the client re-reads the
   task.

## Decision

Option 3, documented as a known deviation from A2A 1.0. The status message says the order
was transmitted and the answer will come later. Clients re-read the task with
`GET /tasks/{id}`, or send `returnImmediately: true`. Push notifications and streaming are
not offered: the pharmacy calls, the supplier answers.

## Consequences

Any A2A client works without special handling. The deviation is stated in the documentation and
the specification rather than hidden.
