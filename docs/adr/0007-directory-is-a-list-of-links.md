# 0007: The directory is a list of links, the product index is computed

**Status:** Proposed

**Date:** 2026-11-23

## Context

The address of each supplier agent is entered by hand in the pharmacy's software. A2A defines
no registry: its specification leaves registries open and the question has been discussed
there for over a year without a decision. An integrator asked, in practice, for a way to know
which suppliers sell a given CIP 13 or EAN 13 code without querying every catalogue.

## Options considered

1. A shared product database, maintained centrally.
2. A directory that stores each supplier's data (address, range, terms).
3. A directory that only lists domains, each supplier declaring its SIREN and agent on its
   own domain, with a product index computed from the ranges the suppliers already publish
   in their sheets.
4. A decentralised ledger.

## Decision

Option 3. The supplier stays authoritative on its own products: its range is already in its
supplier sheet, so the index costs nothing to maintain and cannot drift from it. Every claim
of the directory is re-read at the source at each computation, so editing the directory alone
cannot divert an order. The supplier declares itself on its own domain, which is both its
consent and the designation of its agent, hosted or not. No price enters the index. Option 1 needs maintenance and duplicates the supplier's
data; option 2 makes the directory a second source of truth; option 4 adds cost and
complexity without removing the need to verify the supplier's domain.

## Consequences

Anyone can rebuild the index and run their own directory with the same rules: nothing is
locked. A supplier appears in the index only once its agent, card and sheet are in order,
which also makes the index a public list of conforming agents. Nobody validates a
registration by hand, so the directory needs no maintenance. Identity rests on the domain: a
SIREN declared by two domains is set aside for both, and trust in a supplier still comes from
the commercial relationship that gives the pharmacy its account and token. The index tells who lists a code, not
who can deliver it: availability is still asked to the supplier.
