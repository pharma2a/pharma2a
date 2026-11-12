# 0004: The order reference is the order's identity, per customer account

**Status:** Accepted

**Date:** 2026-10-30

## Context

A client that loses the connection after sending an order does not know whether it arrived,
and sends it again. A second transmission must never become a second commercial order: the
supplier would ship twice.

## Options considered

1. Rely on the A2A `messageId` only.
2. Make the order `reference`, chosen by the pharmacy, the identity of the order for a given
   customer account, and give each line its own `id_ligne`.

## Decision

Option 2. The same reference received again from the same customer account returns the task
already open, same id, same state, honouring `historyLength`, and nothing is sent again. The
same reference with different content while the task is open is refused, with the gesture
to make (close the task, then resend). After the pharmacy closes its task, the reference may
be sent again and the supplier is told the new order cancels and replaces the previous one.
The acknowledgement answers line by line with `id_ligne`; the supplier copies the reference
and the line ids onto its invoice.

## Consequences

Retries are safe on any network. Two pharmacies may use the same reference without
interfering, because tokens are bound to customer accounts. Matching an invoice to its order
needs no guesswork.
