# 0001: No economic data of the pharmacy in any message

**Status:** Accepted (revised on 2026-11-03)

**Date:** 2026-10-16

## Context

A pharmacy's purchase prices, margins, order amounts and terms with other suppliers are
its own business, and a supplier that sees them gains leverage in every negotiation. At the
same time, checking an invoice requires the supplier's own prices: its list price, the
discount it grants on the invoice, the net price it will charge. The first version of the
rule said "no price at all", which kept the supplier's own terms out of the exchange too.

## Options considered

1. No price of any kind in any message.
2. Prices in both directions, as in a classic EDI order.
3. The criterion is who the information belongs to: what belongs to the pharmacy never
   leaves; what belongs to the supplier travels as the supplier states it.

## Decision

Option 3. The order, the shelf state and the follow-up carry no price. The supplier sheet may
carry the supplier's list price and VAT rate; the acknowledgement may carry, line by line,
the list price, the discount on the invoice, the net price and the VAT rate, each as the
supplier writes it and never computed by an agent. The schemas reject any unknown field, so
a price cannot slip into a pharmacy message.

## Consequences

The pharmacy's agent can check the acknowledgement, and later the invoice, against what the
supplier itself said, without revealing anything of the pharmacy. Year-end rebates and back
margins stay out of the protocol: they are not on the invoice. The supplier's free-shipping
threshold may be quoted back by the pharmacy, as the supplier stated it, never computed.
