# 0002: Field names, code and comments in French

**Status:** Accepted

**Date:** 2026-10-16

## Context

The dictionary describes French community pharmacy practice: CIP 13, franco, reliquat,
colisage, rupture. These words have precise trade meanings that English approximations blur
("back order" is not quite "reliquat", "free shipping threshold" not quite "franco"). The
first implementers are French: suppliers' order desks, ERP vendors, pharmacy software
vendors.

## Options considered

1. Everything in English, as most open protocols.
2. Field names in French, code in English.
3. Field names, code and comments in French; public documentation in English as well.

## Decision

Option 3. Field names and code stay in the words of the trade, so that the code reads like
the dictionary it implements (`commande.lignes[].quantite_confirmee`, not a mix of both
languages). The README, the specification, these decision records and the extension page
are in English, for the A2A community and any reader outside France.

## Consequences

French implementers read the code in their own trade vocabulary. Contributors who do not
read French face a barrier in the code, though not in the documentation. The question can be
reopened if the protocol is adopted outside France; renaming fields would then be a new
version of the dictionary.
