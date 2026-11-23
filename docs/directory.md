# The directory and the product index

**Status:** draft, not part of PharmA2A 0.1 · **Decision record:** [ADR 0007](adr/0007-directory-is-a-list-of-links.md)

A pharmacy agent needs to find the agent of a supplier, and to know which suppliers sell a
given product. A2A defines no registry. This page defines two public, read-only documents
that answer both questions, and how anyone can rebuild them.

The key words MUST, SHOULD and MAY are used as in the [specification](../SPECIFICATION.md#1-conventions).

## 1. Principles

1.1. The directory is a list of links: for each supplier, its SIREN, its name and its domain.
Everything else is read at the supplier's, which is authoritative: its agent card, its
supplier sheet, its range.

1.2. The product index is computed from the ranges published in the supplier sheets. Nothing
is entered by hand, no price enters it, and anyone can run the same computation and get the
same result.

1.3. Nothing is locked. The directory file, the program that builds the index and the index
itself are public. Anyone MAY run their own directory with the same rules; the reference one
runs at `pharma2a.org`.

## 2. The directory

2.1. The directory is a list of domains:

```json
{"version": "1", "fournisseurs": [
  {"domaine": "labo-exemple.example"},
  {"domaine": "labo-deux.example"}]}
```

2.2. A supplier declares itself on its own domain, with the file
`https://<domaine>/.well-known/pharma2a.json`:

```json
{"siren": "111111111", "agent": "https://agents.integrator.example/labo-deux"}
```

Without `agent`, the supplier agent is at `https://<domaine>`. With `agent`, it is hosted
elsewhere, for example by an integrator. The card is read at `<agent>/.well-known/agent-card.json`,
the sheet at `<agent>/fiche_fournisseur.json`.

2.3. The declaration is the supplier's consent and designates its agent. Nobody can list a
supplier, or point to an agent, without access to the supplier's domain. Removing the file
removes the supplier from the index at the next computation.

## 3. Verification

A supplier is verified, and enters the index, only if all of the following hold. Otherwise it
stays in `fournisseurs.json`, marked not verified, with the reason.

3.1. The declaration exists on the domain and carries a SIREN of 9 digits.

3.2. The agent card declares the extension `https://pharma2a.org/ext/v1`.

3.3. The supplier sheet validates against the dictionary.

3.4. The SIREN of the sheet (`fournisseur.siren`) is the SIREN of the declaration.

3.5. The SIREN is declared by one domain only. When two domains declare the same SIREN,
neither enters the index until it is cleared up: first come is not first served.

The name shown is the one in the supplier sheet (`fournisseur.nom`).

Documents are read over HTTPS, without any token, without following redirects to another
origin. Verification is repeated at every computation, not only at registration.

## 4. The published files

The computation produces static files that any web server can distribute:

| File | Content |
|---|---|
| `index.json` | the date of the computation and the counts |
| `fournisseurs.json` | every supplier of the directory, verified or not, with the reason and the number of references |
| `produits/<code>.json` | for a CIP 13 or EAN 13 code, the verified suppliers that sell it: SIREN, name, agent |

4.1. A reference with a `date_arret` is not indexed: it is no longer offered.

4.2. The index carries no price, no stock, no condition. To know whether a supplier can
deliver, the pharmacy agent asks it, with a proposal ([specification § 7.3](../SPECIFICATION.md#7-orders-and-tasks)).

4.3. A new computation replaces the previous one at once: a reader never sees a half-written
index.

## 5. What a pharmacy agent does

5.1. To find suppliers for a product: read `produits/<code>.json`. A missing file means no
verified supplier lists the code.

5.2. Before ordering, it SHOULD read the supplier's card and sheet itself, at the agent
address, rather than rely on the index alone.

5.3. The index finds an agent; it gives no right to order. Orders still need the token the
supplier issues to the pharmacy ([specification § 11](../SPECIFICATION.md#11-security)).

## 6. Registration

6.1. To register, a supplier (or its integrator, from the supplier's domain) publishes the
declaration of § 2.2 and gives its domain. Nobody validates anything by hand: the
verification of § 3 runs at every computation.

6.2. Leaving is removing the declaration, at any time.

6.3. Registration is free and open to every supplier and every integrator.

6.4. Identity rests on the domain. The index helps find an agent; trust in a supplier still
comes from the commercial relationship, which gives the pharmacy its customer account and its
token (§ 5.3).

## 7. Rebuilding the index

The reference implementation builds it:

```bash
python -m pharma2a.reseau reseau.json --sortie reseau/
```

It prints one line per supplier, verified or set aside with the reason, and writes the files
of § 4 into `reseau/`.

The reference directory is [`reseau.json`](../reseau.json) in this repository. Its index is
recomputed every night and served at `https://annuaire.pharma2a.org/`, for example
`https://annuaire.pharma2a.org/produits/3400000000011.json`.
