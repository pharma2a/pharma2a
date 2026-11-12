"""Les identifiants d'une ligne (cip13, ean13, ref_fournisseur) et leur jointure typée.
Bibliothèque standard seule : le rendu des mails (courrier.py) s'en sert, et l'agent
d'achat de l'officine peut le charger tel quel pour montrer le mail avant de le signer."""
from __future__ import annotations


def code(ligne) -> str:
    """L'identifiant d'une ligne pour l'œil et les jointures : cip13, sinon ean13, sinon la
    référence du fournisseur. Vide si la ligne n'en a aucun (le schéma la refuse)."""
    if not isinstance(ligne, dict):
        return ""
    return str(ligne.get("cip13") or ligne.get("ean13") or ligne.get("ref_fournisseur") or "")


IDENTIFIANTS = ("cip13", "ean13", "ref_fournisseur")


def code_affiche(ligne) -> str:
    """Le code tel qu'on le montre, avec sa nature quand ce n'est pas un CIP (une colonne « CIP13 » affichait un EAN ou une référence fournisseur sans le dire)."""
    if not isinstance(ligne, dict):
        return ""
    if ligne.get("cip13"):
        return str(ligne["cip13"])
    if ligne.get("ean13"):
        return f"EAN {ligne['ean13']}"
    return f"Réf. {ligne['ref_fournisseur']}" if ligne.get("ref_fournisseur") else ""


def cles(ligne) -> list[tuple[str, str]]:
    """Les identifiants d'une ligne, typés : un CIP ne se compare qu'à un CIP, une référence
    fournisseur qu'à une référence fournisseur, même si les chiffres se ressemblent."""
    if not isinstance(ligne, dict):
        return []
    return [(k, str(ligne[k])) for k in IDENTIFIANTS if ligne.get(k)]


AMBIGU = object()     # un identifiant porté par deux lignes : il ne désigne plus rien


def indexer(lignes) -> dict:
    """(type, valeur) -> ligne, pour retrouver une ligne par n'importe lequel de ses identifiants.
    Un identifiant que deux lignes portent est marqué ambigu : il ne retrouve rien."""
    index = {}
    for l in lignes or []:
        if isinstance(l, dict):
            for c in cles(l):
                index[c] = AMBIGU if (c in index and index[c] is not l) else l
    return index


def retrouver(index: dict, ligne):
    """La ligne de l'index que désignent les identifiants de celle-ci, ou None : rien si aucun
    ne correspond, rien non plus s'ils se contredisent (le CIP d'une ligne, l'EAN d'une autre)
    ou si l'un d'eux est ambigu."""
    trouves = []
    for c in cles(ligne):
        if c in index:
            v = index[c]
            if v is AMBIGU:
                return None
            if not any(v is x for x in trouves):
                trouves.append(v)
    return trouves[0] if len(trouves) == 1 else None
