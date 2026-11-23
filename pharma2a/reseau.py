"""Le réseau : l'annuaire des partenaires et l'index des produits.

L'annuaire est une liste de domaines, rien de plus. Tout se lit CHEZ le fournisseur, qui fait
foi : sa déclaration, sa carte, sa fiche, sa gamme. Ce programme fait le tour de l'annuaire,
vérifie chaque fournisseur, et en tire l'index : pour un code CIP 13 ou EAN 13, quels
fournisseurs le vendent. Rien n'est saisi à la main, aucun prix n'y entre, personne n'a à
valider une inscription, et n'importe qui peut refaire le même calcul.

    python -m pharma2a.reseau reseau.json --sortie dossier/

L'annuaire (JSON) : {"version": "1", "fournisseurs": [{"domaine": "labo-exemple.example"}, ...]}

S'inscrire, c'est déposer sur son propre domaine le fichier
https://<domaine>/.well-known/pharma2a.json, {"siren": "...", "agent": "https://..."} (sans
« agent », l'agent est sur le domaine lui-même), puis donner ce domaine. Le fichier vaut
consentement et désigne l'agent : personne ne peut inscrire un fournisseur à une adresse qu'il
n'a pas choisie, et le retirer suffit à sortir de l'index au calcul suivant.

Un fournisseur n'entre dans l'index que si tout concorde : la déclaration, la carte qui déclare
l'extension PharmA2A, la fiche conforme au dictionnaire, le même SIREN dans la déclaration et
la fiche, et ce SIREN revendiqué par un seul domaine.

La sortie, des fichiers statiques qu'un simple serveur web distribue :
  fournisseurs.json        chaque fournisseur de l'annuaire, vérifié ou non, avec la raison
  produits/<code>.json     pour un code, les fournisseurs vérifiés qui le vendent
  index.json               la date du calcul et les comptes"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

from . import officine
from .dictionnaire import EXTENSION, valider

CODE = re.compile(r"^[0-9]{13}$")
SIREN = re.compile(r"^[0-9]{9}$")
DOMAINE = re.compile(r"^(localhost|127\.0\.0\.1|[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+)(:[0-9]{1,5})?$")
TAILLE_MAX = 5_000_000          # une fiche plus grosse n'est pas lue


def lire(url: str, delai: int = 15) -> dict:
    """Un document JSON public. Jamais de jeton, HTTPS seulement (HTTP vers la machine elle-même,
    pour les essais), aucune redirection vers une autre origine."""
    if not officine._sur(url):
        raise ValueError("adresse sans HTTPS")
    local = officine.origine(url)[1] in officine._LOCAL
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with (officine._OUVREUR_LOCAL if local else officine._OUVREUR).open(req, timeout=delai) as r:
            brut = r.read(TAILLE_MAX + 1)
    except urllib.error.HTTPError as e:
        raise ValueError(f"HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise ValueError(f"pas de réponse ({getattr(e, 'reason', e)})")
    if len(brut) > TAILLE_MAX:
        raise ValueError("document trop gros")
    try:
        doc = json.loads(brut)
    except ValueError:
        raise ValueError("ce n'est pas du JSON")
    if not isinstance(doc, dict):
        raise ValueError("ce n'est pas un objet JSON")
    return doc


def _base(domaine: str) -> str:
    local = domaine.split(":")[0] in officine._LOCAL
    return ("http://" if local else "https://") + domaine


def verifier(entree: dict) -> dict:
    """Un fournisseur de l'annuaire, vérifié chez lui. Rend {siren, nom, domaine, agent, verifie,
    raison, codes}. L'annuaire ne donne qu'un domaine : le SIREN et l'agent sont ceux que le
    fournisseur déclare sur son propre domaine, le nom celui de sa fiche."""
    domaine = str(entree.get("domaine") or "").lower()
    out = {"siren": "", "nom": "", "domaine": domaine, "agent": None, "verifie": False, "raison": None, "codes": []}
    if not DOMAINE.match(domaine):
        return {**out, "raison": "domaine invalide dans l'annuaire"}
    try:
        preuve = lire(_base(domaine) + "/.well-known/pharma2a.json")
    except ValueError as e:
        return {**out, "raison": f"pas de déclaration sur le domaine (/.well-known/pharma2a.json) : {e}"}
    siren = str(preuve.get("siren") or "")
    if not SIREN.match(siren):
        return {**out, "raison": "la déclaration du domaine n'a pas de SIREN (9 chiffres)"}
    agent = str(preuve.get("agent") or _base(domaine)).rstrip("/")
    out |= {"siren": siren, "agent": agent}
    try:
        carte = lire(agent + "/.well-known/agent-card.json")
    except ValueError as e:
        return {**out, "raison": f"carte de l'agent illisible : {e}"}
    if EXTENSION not in [x.get("uri") for x in (carte.get("capabilities") or {}).get("extensions") or [] if isinstance(x, dict)]:
        return {**out, "raison": "la carte ne déclare pas l'extension PharmA2A"}
    try:
        fiche = lire(agent + "/fiche_fournisseur.json")
    except ValueError as e:
        return {**out, "raison": f"fiche du fournisseur illisible : {e}"}
    erreurs = valider("fiche_fournisseur", fiche)
    if erreurs:
        return {**out, "raison": "fiche hors dictionnaire : " + "; ".join(erreurs[:2])}
    f = fiche.get("fournisseur") or {}
    out["nom"] = str(f.get("nom") or "")
    if str(f.get("siren")) != siren:
        return {**out, "raison": "le SIREN de la fiche n'est pas celui que déclare le domaine"}
    codes = set()
    for l in fiche.get("gamme") or []:
        if l.get("date_arret"):
            continue                          # une référence arrêtée ne se propose plus
        codes.update(str(l[k]) for k in ("cip13", "ean13") if l.get(k) and CODE.match(str(l[k])))
    return {**out, "verifie": True, "codes": sorted(codes)}


def construire(annuaire: dict, sortie: pathlib.Path) -> dict:
    """Fait le tour de l'annuaire et écrit les fichiers. L'index précédent est remplacé d'un
    coup : un lecteur ne voit jamais un index à moitié écrit."""
    quand = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    fournisseurs, domaines = [], set()
    for e in annuaire.get("fournisseurs") or []:
        f = verifier(e) if isinstance(e, dict) else {"verifie": False, "raison": "entrée invalide", "siren": "", "nom": "", "domaine": "", "agent": None, "codes": []}
        if f["domaine"] and f["domaine"] in domaines:
            continue                          # le même domaine inscrit deux fois : une seule ligne
        domaines.add(f["domaine"])
        fournisseurs.append(f)
    # un SIREN revendiqué par deux domaines : aucun des deux n'entre, plutôt que le premier arrivé
    par_siren: dict[str, int] = {}
    for f in fournisseurs:
        if f["verifie"]:
            par_siren[f["siren"]] = par_siren.get(f["siren"], 0) + 1
    fournisseurs = [{**f, "verifie": False, "codes": [], "raison": "SIREN déclaré par plusieurs domaines : à éclaircir"}
                    if f["verifie"] and par_siren[f["siren"]] > 1 else f for f in fournisseurs]
    produits: dict[str, list[dict]] = {}
    for f in fournisseurs:
        for c in f["codes"]:
            produits.setdefault(c, []).append({"siren": f["siren"], "nom": f["nom"], "agent": f["agent"]})
    neuf = sortie.with_name(sortie.name + ".neuf")
    (neuf / "produits").mkdir(parents=True, exist_ok=True)
    ecrire = lambda p, d: p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    ecrire(neuf / "fournisseurs.json", {"version": "1", "calcule_le": quand, "fournisseurs": [
        {k: f.get(k) for k in ("siren", "nom", "domaine", "agent", "verifie", "raison")} | {"references": len(f["codes"])} for f in fournisseurs]})
    for code, qui in produits.items():
        ecrire(neuf / "produits" / f"{code}.json", {"version": "1", "code": code, "calcule_le": quand, "fournisseurs": qui})
    resume = {"version": "1", "calcule_le": quand, "fournisseurs_inscrits": len(fournisseurs),
              "fournisseurs_verifies": sum(1 for f in fournisseurs if f["verifie"]), "codes": len(produits)}
    ecrire(neuf / "index.json", resume)
    ancien = sortie.with_name(sortie.name + ".ancien")
    if sortie.exists():
        sortie.rename(ancien)
    neuf.rename(sortie)
    if ancien.exists():
        for p in sorted(ancien.rglob("*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()
        ancien.rmdir()
    return resume | {"detail": fournisseurs}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m pharma2a.reseau", description=__doc__.split("\n\n")[0])
    p.add_argument("annuaire", help="le fichier JSON de l'annuaire des partenaires")
    p.add_argument("--sortie", default="reseau", help="le dossier où écrire l'index (défaut : reseau)")
    a = p.parse_args(argv)
    try:
        annuaire = json.loads(pathlib.Path(a.annuaire).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"annuaire illisible : {e}", file=sys.stderr)
        return 2
    r = construire(annuaire, pathlib.Path(a.sortie))
    for f in r["detail"]:
        print(f"{'vérifié   ' if f['verifie'] else 'ÉCARTÉ    '}{f.get('siren', ''):<11}{officine.propre(f.get('nom', ''))[:40]:<42}"
              + (f"{len(f['codes'])} référence(s)" if f["verifie"] else officine.propre(f["raison"])))
    print(f"\n{r['fournisseurs_verifies']} fournisseur(s) vérifié(s) sur {r['fournisseurs_inscrits']}, {r['codes']} code(s) dans l'index, écrit dans {a.sortie}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
