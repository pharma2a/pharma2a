# Changelog

Each version of PharmA2A is listed here, the most recent first. The dictionary has its own
version number, carried in every message and in the extension URI; see section 12 of
[`SPECIFICATION.md`](SPECIFICATION.md) for how it changes.

## 0.1 (12 November 2026)

First public version. Dictionary version 1, extension URI `https://pharma2a.org/ext/v1`.

**The protocol**

- Five messages, each a JSON Schema: `fiche_fournisseur`, `commande` (with the proposal
  before the order), `accuse_reception`, `etat_de_rayon` and `relance`.
- The specification, [`SPECIFICATION.md`](SPECIFICATION.md), and the decision records in
  [`docs/adr/`](docs/adr/README.md).
- Declared as an A2A 1.0 profile extension, with `required: false`.

**The code**

- A sample supplier agent in about a hundred lines, `examples/sample-supplier`.
- The supplier email bridge, the `pharma2a` package: agent card, supplier sheet served
  without a key, one token per pharmacy, orders sent by mail and replies read back as
  acknowledgements, several agents in one process, a draft mode that sends nothing.
- An ERP channel that exchanges files, with an example in `pharma2a/backends/erp_exemple.py`.
- A demo pharmacy on the command line, `python -m pharma2a.officine`.

**Conformance**

- Checked against the official A2A conformance kit on the HTTP+JSON and JSON-RPC bindings;
  the results are in [`docs/protocol.md`](docs/protocol.md#protocol-and-a2a-conformance).
- Dependencies are pinned to the exact versions the tests run.

Invoices are out of scope: they go through the certified e-invoicing platforms, which can
carry the `reference` and `id_ligne` of the order.
