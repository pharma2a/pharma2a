# PharmA2A

[![tests](https://github.com/pharma2a/pharma2a/actions/workflows/tests.yml/badge.svg)](https://github.com/pharma2a/pharma2a/actions/workflows/tests.yml)
[![A2A 1.0](https://img.shields.io/badge/A2A-1.0-blue)](https://a2a-protocol.org/latest/specification/)
[![A2A extension](https://img.shields.io/badge/A2A%20extension-pharma2a.org%2Fext%2Fv1-blue)](https://pharma2a.org/ext/v1)
[![Dictionary: CC0 1.0](https://img.shields.io/badge/dictionary-CC0%201.0-lightgrey)](schema/LICENSE)
[![Agent: Apache 2.0](https://img.shields.io/badge/agent-Apache%202.0-lightgrey)](pharma2a/LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-lightgrey)](pyproject.toml)

**The open protocol for direct orders between a pharmacy and its suppliers, labs, wholesalers and buying platforms, on top of A2A.**

*Version 0.1, published on 12 November 2026. Site: [pharma2a.org](https://pharma2a.org). Contact: contact@pharma2a.org. [Version française](docs/guide-fr.md).*

In France, a community pharmacy orders directly from hundreds of suppliers: dermocosmetics,
supplements, medical devices. Every order is a form, a mail, a phone call, then a check at
delivery. PharmA2A fixes what the pharmacy's buying agent and the supplier's agent say to
each other, so that an order is right the first time and the supplier learns, when the
pharmacist allows it, what its products do on the shelf.

[A2A](https://a2a-protocol.org) (Agent2Agent, Linux Foundation) says how two agents talk.
PharmA2A says what they say: five messages, each a JSON Schema, declared as an A2A profile
extension, [`https://pharma2a.org/ext/v1`](https://pharma2a.org/ext/v1). To our knowledge,
it is the first sector-specific dictionary built on A2A.

## The five messages

| Message | From | To | What it carries |
|---|---|---|---|
| `fiche_fournisseur` | supplier | anyone, no key | who the supplier is, its terms, its range by CIP 13 with pack size, status and VAT rate, and its list price if it chooses |
| `commande` | pharmacy | supplier | the purchase order: products, quantities, a reference; first as a proposal when the terms are unknown; never a price |
| `accuse_reception` | supplier | pharmacy | line by line: confirmed, back order, out of stock, substitution; order number, delivery date; its own prices as it states them |
| `etat_de_rayon` | pharmacy | supplier | only the supplier's products, only if the pharmacist allows it: stock, sales, cover |
| `relance` | pharmacy | supplier | a follow-up on an order already sent; never a new order |

The field names are French: PharmA2A starts with French community pharmacy, its CIP 13 codes
and its words. The specification and the English guide explain every message in English.

## Rules that do not bend

1. **No economic data of the pharmacy in any message.** The supplier's own terms travel as it states them, never recomputed.
2. **Nothing invented.** A field nobody stated stays `null`.
3. **Only the supplier speaks for the supplier**, and a transcribed answer never confirms an order on its own.
4. **A message that says nothing is not a confirmation.**
5. **The pharmacy calls, the supplier answers.** No push, no stream.
6. **No patient data.**

An order is transmitted once: the reference is its identity, per customer account.

## What is in this repository

| Part | What it is | Where |
|---|---|---|
| **The protocol** | The specification and the five JSON Schemas. This is PharmA2A. | [`SPECIFICATION.md`](SPECIFICATION.md), [`schema/`](schema/) |
| **Sample supplier** | The smallest supplier agent that speaks PharmA2A, about a hundred lines: an order in, an acknowledgement out. A template for a lab or a software vendor. | [`examples/sample-supplier/`](examples/sample-supplier/) |
| **Supplier email bridge** | A complete supplier-side agent for suppliers who only have a mailbox today: it sends the order by mail, reads the answer, and returns it as an acknowledgement. It can also exchange files with an ERP. Replies are transcribed by a language model, optional: the model's provider receives the text of the reply and, to match it, the order's reference and lines (product codes, labels, quantities); the agent never adds the pharmacy's identity or a price. | the `pharma2a` package, `python -m pharma2a` |
| **Demo pharmacy** | A command line that sends an order to any supplier agent and shows the acknowledgement. | `python -m pharma2a.officine` |

The `pharma2a` package also carries what every part uses: the dictionary and its validation (`pharma2a.dictionnaire`).

## Quick start

Python 3.11 or newer.

```bash
git clone https://github.com/pharma2a/pharma2a && cd pharma2a
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[modele,test]"
```

**The sample supplier, in one minute.** Start it, then let the demo pharmacy order from it:

```bash
python examples/sample-supplier/agent.py
python -m pharma2a.officine --agent http://127.0.0.1:8766 --attendre 2    # in a second terminal
```

**The supplier email bridge.** It starts in draft mode: nothing is sent, mails are written to `sorties/`.

```bash
PHARMA2A_JETON=a-test-token python -m pharma2a
PHARMA2A_JETON=a-test-token python -m pharma2a.officine       # in a second terminal
```

Its agent card is at `http://127.0.0.1:8765/.well-known/agent-card.json`. Every setting is an
environment variable, listed in `.env.example`. `python -m pytest` runs the tests.

## Documentation

- [**Specification**](SPECIFICATION.md): the normative rules, for anyone who implements PharmA2A.
- [**The protocol in detail**](docs/protocol.md): the messages, the diagrams, the rules, A2A conformance.
- [**Connecting a supplier system**](docs/erp.md): the backend contract, an ERP round trip in 10 minutes, the dictionary mapped to ERP fields.
- [**Decision records**](docs/adr/README.md): why the protocol is the way it is.
- [**The guide**](docs/guide.md): the complete guide for a supplier's IT staff, installation and configuration included. [Version française](docs/guide-fr.md).
- The dictionary itself: [`schema/`](schema/), with an example of each message in [`schema/exemples/`](schema/exemples/).

## Where it is used

The pharmacy buying agent that SyderaAI (Paris) runs for its pharmacies speaks PharmA2A with
their suppliers. No supplier runs an agent yet, so that buying agent hosts the supplier email bridge for each supplier: the supplier gets a clear mail with the order in its body, answers as usual,
and the bridge transcribes the answer into the dictionary. The day a supplier serves its own
agent, the pharmacy changes one URL.

## Agents

Pharmacy buying agents that speak PharmA2A:

| Agent | Publisher |
|---|---|
| Mercury | SyderaAI, Paris |

To add yours, open a pull request that adds one line to this table: the agent's name, its
publisher, and a contact. Pour ajouter le vôtre, ouvrez une pull request qui ajoute une
ligne à ce tableau.

## Contributing

Issues and pull requests are welcome: see [`CONTRIBUTING.md`](CONTRIBUTING.md),
[`GOVERNANCE.md`](GOVERNANCE.md) and [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md). Security
reports go through [`SECURITY.md`](SECURITY.md), never through a public issue. Versions are in
[`CHANGELOG.md`](CHANGELOG.md).

## Licences and name

The dictionary (`schema/`) is public domain, CC0 1.0. The agent and its tests are Apache 2.0.
PharmA2A is a trademark of SyderaAI, Paris, filed with the French INPI: the content is free,
the name is not, see [`TRADEMARK.md`](TRADEMARK.md). Anyone may implement, copy, fork and extend
the protocol without asking; the name is protected only so that nobody can call "PharmA2A" what
does not follow it, which is what keeps the common language common.
