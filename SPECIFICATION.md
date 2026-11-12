# PharmA2A specification

**Version:** 0.1 (dictionary version 1) · **Published:** 2026-11-12 ·
**Extension URI:** `https://pharma2a.org/ext/v1` · **Status:** stable

This document states what an implementation MUST do to speak PharmA2A. The JSON Schemas in
[`schema/`](schema/) are normative for the content of each message; this document is
normative for everything else. The reasons behind the rules are in
[`docs/adr/`](docs/adr/README.md); a guided tour is in [`docs/protocol.md`](docs/protocol.md).

## 1. Conventions

The key words MUST, MUST NOT, REQUIRED, SHOULD, SHOULD NOT, RECOMMENDED, MAY and OPTIONAL are
to be interpreted as described in [RFC 2119](https://datatracker.ietf.org/doc/html/rfc2119)
and [RFC 8174](https://datatracker.ietf.org/doc/html/rfc8174) when, and only when, they appear
in capitals.

Field names are in French, by design ([ADR 0002](docs/adr/0002-french-field-names-and-code.md)).

## 2. Roles

- **Pharmacy agent**: acts for a community pharmacy. It sends `commande`, `etat_de_rayon`
  and `relance`, and reads `fiche_fournisseur` and `accuse_reception`. It is the A2A client.
- **Supplier agent**: acts for a supplier (laboratory, wholesaler, buying platform). It
  publishes `fiche_fournisseur`, receives the pharmacy's messages, passes them to the
  supplier, and returns `accuse_reception`. It is the A2A server.
- **Transcribing agent**: a supplier agent that reaches the supplier through a human channel
  (mail) and transcribes the replies. The reference implementation's mail bridge is one.

## 3. Relationship to A2A

3.1. A supplier agent MUST implement [A2A 1.0](https://a2a-protocol.org/latest/specification/)
with at least one binding declared in its agent card. The reference implementation offers
HTTP+JSON and JSON-RPC.

3.2. A supplier agent SHOULD declare the extension in `capabilities.extensions` of its agent
card, with `uri` set to `https://pharma2a.org/ext/v1`. Version 1 declares it with
`required: false` ([ADR 0006](docs/adr/0006-optional-a2a-profile-extension.md)).

3.3. A pharmacy agent MAY request the extension with the `A2A-Extensions` header and MAY list
it in `Message.extensions`. A supplier agent MUST serve a client that does not request it in
the same way, and MUST ignore extensions it does not know, as A2A requires.

3.4. A supplier agent SHOULD list the extension URI in `Artifact.extensions` of every
artifact that carries a dictionary message.

3.5. A supplier agent MAY set `streaming` and `pushNotifications` to `false`. A pharmacy agent
MUST NOT rely on either: it re-reads tasks with `GetTask` (the pharmacy calls, the supplier
answers).

## 4. Messages

4.1. The dictionary has five messages, each defined by a JSON Schema (draft 2020-12):

| Message | Schema | Sender | Media type |
|---|---|---|---|
| `fiche_fournisseur` | `schema/fiche_fournisseur.json` | supplier | `application/pharma2a.fiche_fournisseur+json` |
| `commande` | `schema/commande.json` | pharmacy | `application/pharma2a.commande+json` |
| `accuse_reception` | `schema/accuse_reception.json` | supplier | `application/pharma2a.accuse_reception+json` |
| `etat_de_rayon` | `schema/etat_de_rayon.json` | pharmacy | `application/pharma2a.etat_de_rayon+json` |
| `relance` | `schema/relance.json` | pharmacy | `application/pharma2a.relance+json` |

4.2. A message MUST travel as a data part (`Part.data`) of an A2A message or artifact, with
`mediaType` set to the media type of its kind. The metadata key `pharma2a.message` MAY name
the kind as well.

4.3. Every message MUST validate against its schema. Schemas reject unknown fields; an
implementation MUST NOT add fields. A supplier agent MUST reject a message that does not
validate, ending the task in `TASK_STATE_REJECTED` with the reason in the status message, and
MUST NOT pass it to the supplier.

4.4. A field that nobody stated MUST be `null` or absent. An implementation MUST NOT infer,
compute or default a value the sender did not give, in particular a delivery date, a
quantity, an order number or a price.

4.5. Integers MAY arrive as floats with a zero fraction (`6.0`), because of the protobuf
transport; implementations MUST accept them as integers.

4.6. A product line MUST carry at least one of `cip13`, `ean13`, `ref_fournisseur`. An order
MUST NOT carry the same product on two lines.

## 5. Data rules

5.1. No message MUST carry economic data of the pharmacy: purchase price, retail price,
margin, order amount, terms with another supplier, purchase channel
([ADR 0001](docs/adr/0001-no-pharmacy-economic-data.md)). `commande`, `etat_de_rayon` and
`relance` carry no price.

5.2. The supplier's own terms MAY travel as the supplier states them: its list price and VAT
rate in `fiche_fournisseur`; per line in `accuse_reception`, its list price, the discount on
the invoice, the net price and the VAT rate. An agent MUST NOT compute any of them from the
others. A discount that does not appear on the invoice (year-end rebate, back margin) MUST
NOT be reported as `remise_pct`.

5.3. The pharmacy MAY quote back the supplier's free-shipping threshold (`franco_ht`) and pack
sizes as the supplier stated them. It MUST NOT state the order amount.

5.4. No message MUST carry patient data.

5.5. `etat_de_rayon` MUST only be sent if the pharmacist has enabled it for that supplier,
and MUST only cover that supplier's products. A supplier agent MUST reject a shelf state that
names another pharmacy or another supplier than the order it travels with or the agent it
reaches.

## 6. The supplier sheet

6.1. A supplier agent SHOULD serve its `fiche_fournisseur` without authentication at
`GET /fiche_fournisseur.json`, next to its agent card, and MUST return it as an artifact to
an authenticated `SendMessage` carrying `{"version": "1", "demande": "fiche_fournisseur"}`.

6.2. Without authentication, the list price MUST be hidden unless the supplier chose to make
it public. The VAT rate MAY stay visible. A term specific to one pharmacy MUST NOT appear in
the sheet.

6.3. A sheet produced by a transcribing agent MUST only contain what the supplier itself
stated.

## 7. Orders and tasks

7.1. Each `commande` opens one A2A task. A supplier agent MUST use the task states as follows:

| State | Meaning |
|---|---|
| `TASK_STATE_SUBMITTED` | received and validated |
| `TASK_STATE_WORKING` | transmitted to the supplier, waiting for its answer |
| `TASK_STATE_COMPLETED` | the supplier confirmed the order as sent; the acknowledgement is an artifact |
| `TASK_STATE_INPUT_REQUIRED` | the pharmacist must decide: back order, shortage, substitution, changed quantity, changed net price or discount, refusal, or an answer that does not confirm |
| `TASK_STATE_REJECTED` | the message does not follow the dictionary, or the order conflicts with an open one |
| `TASK_STATE_FAILED` | the transmission failed or its outcome is uncertain |
| `TASK_STATE_CANCELED` | closed by the pharmacy |

7.2. A supplier agent MAY answer `SendMessage` as soon as the order has left, in
`TASK_STATE_WORKING`, even when `returnImmediately` is `false`, and SHOULD say so in the status
message ([ADR 0005](docs/adr/0005-answer-in-working.md)). This is a known deviation from A2A
1.0.

7.3. `nature: "proposition"` is a quote request: the supplier agent MUST NOT place an order.
The answer is an `accuse_reception` that gives pack sizes and terms.

7.4. Closing a task (`CancelTask`) MUST NOT be presented as recalling an order already
transmitted. A message sent on an existing task MUST NOT transmit anything to the supplier.

## 8. Idempotence

8.1. `reference` is the identity of an order for a given customer account
([ADR 0004](docs/adr/0004-order-reference-is-idempotent.md)). A supplier agent MUST NOT
transmit the same reference twice for the same account while its task is open: the same
reference received again MUST return the task already open, honouring `historyLength`.

8.2. The same reference with different content while the task is open MUST be rejected, with
the reason. After the task is closed, failed or rejected, the reference MAY be sent again; the
supplier SHOULD be told that the new order cancels and replaces the previous one.

8.3. Each line carries an `id_ligne`, unique in the order. The acknowledgement MUST answer by
`id_ligne`. The supplier SHOULD copy `reference` and `id_ligne` onto its invoice.

## 9. Acknowledgements

9.1. Every `accuse_reception` MUST state its `origine`: `erp` when a supplier system produced
it, `transcription` when an agent read a human reply. The origin MUST be set by the channel,
never by the content.

9.2. A pharmacy agent MUST NOT accept a `transcription` acknowledgement as an automatic
confirmation: it goes to the pharmacist
([ADR 0003](docs/adr/0003-transcription-never-confirms.md)).

9.3. An acknowledgement MUST answer the order it names: same `reference`, `id_ligne` values
that exist in the order and do not repeat, product codes that are those of the designated
line. Otherwise a supplier agent MUST discard it.

9.4. An acknowledgement that carries an order number, a delivery date or
`confirmation_globale: true` records the order: lines it does not list are taken as ordered.
Without them, a line confirmed does not confirm the others. An answer with no line, no order
number, no delivery date and no term is not a confirmation, and the task stays as it was.

9.5. A transcribing agent MUST only read a reply that comes from the domain the order was
sent to, SHOULD refuse a reply whose sender failed authentication (DKIM, SPF, DMARC) as
reported by a trusted receiving server, and MUST NOT treat a forward, a copy or a message
sent from the pharmacy as the supplier's answer.

## 10. Later answers and follow-ups

10.1. A supplier may write again after a first answer. A supplier agent SHOULD keep listening
to an answered task for 30 days. A later answer that changes something the pharmacist relies
on (a shortage, a quantity, a delivery date, a refusal, a net price or a discount) MUST put
the task back to the pharmacist; an answer that only completes missing fields MUST NOT change
its state.

10.2. A terminal task MUST NOT be reopened. A later answer to a completed task MUST open a
follow-up task in the same `contextId`, naming the first one in its metadata (`suite_de`) and
in `referenceTaskIds` of its first message. A pharmacy agent SHOULD follow the chain and read
the latest acknowledgement.

10.3. A `relance` MUST refer to an order the same supplier agent received under the same
reference for the same pharmacy, and MUST NOT place a new order or change products or
quantities. Its `motif` is `livraison_en_retard` or `sans_reponse`.

## 11. Security

11.1. Everything except the agent card and the supplier sheet MUST require authentication. A
bearer token issued by the supplier to each pharmacy, bound to the pharmacy's customer
account, is RECOMMENDED.

11.2. A token bound to an account MUST only see, list and act on that account's tasks, and an
order MUST only be placed for that account.

11.3. Connections to mail servers and other remote services MUST verify certificates.

## 12. Versioning

12.1. The dictionary changes by versions, never silently. An added field MUST be optional. A
removed field, or a field made required, makes a new version of the schema and a new
extension URI (`https://pharma2a.org/ext/v2`).

12.2. An agent that speaks version N SHOULD keep understanding version N minus one.

## 13. Conformance

An implementation conforms to PharmA2A 0.1 if it meets every MUST of this document for its
role and validates every message against the schemas of dictionary version 1. A conforming
implementation may say it speaks PharmA2A; it may not call itself PharmA2A
([`TRADEMARK.md`](TRADEMARK.md)). The reference implementation in this repository is checked
by its test suite and by the official A2A conformance kit (see
[`docs/protocol.md`](docs/protocol.md#protocol-and-a2a-conformance)).
