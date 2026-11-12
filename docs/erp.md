# Connecting a supplier system

[Back to the README](../README.md) · [Specification](../SPECIFICATION.md) · [Guide en français](guide-fr.md#brancher-son-propre-système)

## Installing and running

`pip install git+https://github.com/pharma2a/pharma2a` works as well as a clone: the
dictionary (`schema/`) is installed with the agent. The Gmail service-account sender needs
`pip install ".[gmail]"`, the model that transcribes mail replies `pip install ".[modele]"`.
The supplier sheet is served at `/fiche_fournisseur.json` once `PHARMA2A_FICHE` points at
one (404 until then). Every setting is an environment variable, listed in `.env.example`.

In production, the supplier issues one token per pharmacy, bound to its customer account:
`python -m pharma2a jeton creer --compte …` (see
[Les jetons des officines](guide-fr.md#les-jetons-des-officines)).

## The backend contract

The agent talks to the supplier through one backend, chosen by `PHARMA2A_BACKEND`. `mail`
is provided: it renders the order as a mail and has a model transcribe the human reply.
A supplier that connects its ERP writes `pharma2a/backends/erp.py` with a class
`Backend(config)` exposing exactly:

| Member | What it does |
|---|---|
| `origine = "erp"` | the acknowledgements come from the supplier's system, not from a reading |
| `transmettre(message, doc, reference, message_id) -> str` | receives the document the agent has already validated against the dictionary: `message` is `"commande"` (with its `etat_de_rayon` if attached), `"etat_de_rayon"` or `"relance"`; `message_id` is unique to this sending; returns the id the ERP gives it |
| `lire_reponses(depuis=None) -> list[Reponse]` | each `Reponse` carries `accuse`, the `accuse_reception` document answering line by line with the order's `id_ligne`, `reference`, and `en_reponse_a=[message_id]`, the sending it answers: two pharmacies may use the same reference, and without it their acknowledgements stay ambiguous; reading must not mark anything |
| `acquitter(identifiants) -> None` | these replies are processed, do not return them again |

The agent validates each acknowledgement against the schema and checks it against the
order (same reference, line ids that exist, codes carried by the line); no model reads it.
`pharma2a/backends/erp_exemple.py` is a working reference that exchanges JSON files through
two folders, one file per sending, the reply citing the sending it answers
(`{"en_reponse_a": "<message_id>", "accuse": {...}}`): copy it to `erp.py`, replace its three methods with your ERP's calls, set
`PHARMA2A_BACKEND=erp`. A supplier may also write its own agent from scratch, in any
language, as long as it speaks A2A 1.0 and the dictionary.

## Try it with an ERP in 10 minutes

The reference ERP backend exchanges JSON files through two folders, which is enough to see
the whole round trip before writing a line of code. Two terminals, from the repository root.

**Terminal 1: the agent, with the file-based ERP**

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e .
export PHARMA2A_JETON=a-test-token PHARMA2A_BACKEND=erp_exemple PHARMA2A_BASE=taches.sqlite
python -m pharma2a
```

**Terminal 2: the demo pharmacy sends the example order**

```bash
. .venv/bin/activate
export PHARMA2A_JETON=a-test-token PHARMA2A_BACKEND=erp_exemple PHARMA2A_BASE=taches.sqlite
python -m pharma2a.officine --attendre 3
ls erp/sortie
```

`pharma2a.officine` is a demo pharmacy: it reads the agent card, sends the example order
under a fresh `DEMO-…` reference and shows the task. The task is in `TASK_STATE_WORKING`, and
the validated order is in `erp/sortie/commande-<message_id>.json`:
`{"message", "message_id", "reference", "document"}`. This is what your ERP reads. The same
command tests any PharmA2A supplier agent: `python -m pharma2a.officine --agent <URL> --jeton
<token>`, or `--commande my-order.json` for your own order.

**Your ERP answers.** Here, the example acknowledgement (one line confirmed, one back
order, one out of stock), dropped in `erp/entree/` with the sending it answers:

```bash
python - <<'PY'
import glob, json, pathlib
envoi = json.load(open(glob.glob("erp/sortie/commande-*.json")[0]))
accuse = json.load(open("schema/exemples/accuse_reception.json"))
accuse["reference"] = envoi["reference"]
pathlib.Path("erp/entree").mkdir(parents=True, exist_ok=True)
json.dump({"en_reponse_a": envoi["message_id"], "accuse": accuse}, open("erp/entree/retour.json", "w"))
PY
python -m pharma2a --relire
python -m pharma2a.officine --tache <task id>
```

`--relire` runs one reading pass now (the agent also reads on its own every
`PHARMA2A_RELECTURE_MINUTES`). The task is in `TASK_STATE_INPUT_REQUIRED`, because of the
back order and the shortage, with the acknowledgement as an artifact and the supplier's order
number `SO-884512`; the file has moved to `erp/entree/traites/`. An acknowledgement that
confirms every line as ordered ends the task in `TASK_STATE_COMPLETED`.

**Then plug your ERP.** Copy `pharma2a/backends/erp_exemple.py` to `pharma2a/backends/erp.py`,
set `PHARMA2A_BACKEND=erp`, and replace the three methods with your ERP's calls. With an ERP
that has an API (Odoo, SAP, Sage X3…): `transmettre` creates the sales order from the
`document` and returns its number, `lire_reponses` returns the orders your team has confirmed
as `accuse_reception` documents citing the `message_id` they answer, and `acquitter` marks
them as passed on. The agent keeps everything else: the A2A side, tokens, validation against
the dictionary, idempotence, the task lifecycle.

## Mapping the dictionary to an ERP

What each field usually is in an ERP, and who decides. The pharmacy never sends a price; the
supplier's own prices, if it gives them, go back in the acknowledgement.

**The order (`commande`), read by the ERP**

| Field | In the ERP |
|---|---|
| `officine.numero_compte_client` | the customer account: the first key to find the customer. May be null when the pharmacy does not know it; then `officine.cip_officine` (the pharmacy's 7-digit identifier), name and address |
| `officine.adresse_livraison` | the delivery address of this order (one per order) |
| `reference` | the customer's order reference: copy it to the sales order, and to the invoice's order reference field |
| `nature` | `commande`: a sales order to execute; `proposition`: a quote request, nothing to ship, answer the `questions` |
| `lignes[].cip13`, `ean13`, `ref_fournisseur` | the product: CIP 13 or EAN 13 is usually the product barcode, `ref_fournisseur` your internal reference; at least one is there. The label is indicative only |
| `lignes[].id_ligne` | the customer's line reference: keep it on the order line, the acknowledgement and the invoice line answer with it |
| `lignes[].quantite` | units of sale (boxes, bottles), never cases |
| `reliquat_accepte` (order or line) | whether a back order may follow without asking; `true` or `false`, null if not said |
| `date_livraison_exigee` | `true`: the pharmacy waits for a firm delivery date in the acknowledgement |
| `message` | free text from the pharmacist for your order desk |

**The acknowledgement (`accuse_reception`), written by the ERP**

| Field | From the ERP |
|---|---|
| `reference`, `lignes[].id_ligne` | copied from the order, unchanged: they are how the answer is matched |
| `origine` | always `erp` for a system answer |
| `numero_commande_labo` | your sales order number |
| `date_livraison_prevue` | the planned delivery date |
| `lignes[].quantite_confirmee` | what ships now; `0` if nothing ships |
| `lignes[].reliquat` | what ships later (back order); `0` if nothing |
| `lignes[].rupture` | `true` when you cannot deliver the product at all |
| `lignes[].cip13_substitue` | a product you propose instead; the pharmacist decides |
| `lignes[].prix_tarif_ht`, `remise_pct`, `prix_net_ht`, `taux_tva` | your list price, the discount on the invoice, the net unit price and the VAT rate, as you will invoice them; never computed by the agent |
| `confirmation_globale` | `true` when the whole order is accepted as is |

Lines you do not list are taken as ordered when the acknowledgement carries an order number or
a delivery date. Anything unknown stays `null`: the agent never fills in a field.
