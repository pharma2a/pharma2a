"""Les jetons des officines : chaque officine appelle l'agent avec un jeton porteur que le
fournisseur lui a délivré, rattaché à son numéro de compte client. L'agent refuse une
commande dont le compte n'est pas celui du jeton, et une officine ne voit que ses tâches.

Le registre est un fichier JSON (PHARMA2A_JETONS) qui ne garde que l'empreinte SHA-256 de
chaque jeton, jamais le jeton lui-même : le fournisseur le donne une fois à l'officine, et
s'il le perd, il en crée un autre. Le fichier est relu quand il change : un jeton révoqué
est refusé à la requête suivante, sans redémarrage.

  python -m pharma2a jeton creer --compte C-0001 --nom "Pharmacie Exemple"
  python -m pharma2a jeton lister
  python -m pharma2a jeton revoquer --compte C-0001

Forme du fichier :
{"jetons": [{"compte_client": "C-0001", "nom": "Pharmacie Exemple",
             "empreinte": "sha256:…", "cree_le": "2026-11-12"}]}"""
from __future__ import annotations
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import pathlib
import secrets
import tempfile

log = logging.getLogger("pharma2a.jetons")
PREFIXE = "p2a_"          # un jeton se reconnaît dans un journal ou un scanner de secrets


def empreinte(jeton: str) -> str:
    return "sha256:" + hashlib.sha256(jeton.encode("utf-8")).hexdigest()


def normal(compte) -> str:
    """Un numéro de compte se compare sans casse ni espaces superflus, comme les identités
    du dictionnaire."""
    return " ".join(str(compte or "").split()).lower()


def officine_du_compte(compte) -> str:
    """L'officine que prouve un jeton : « compte:<numéro> », ou vide pour le jeton unique."""
    return ("compte:" + normal(compte)) if compte else ""


def officine_de(meta: dict) -> str:
    """L'officine d'une tâche, telle que son jeton l'a prouvée à la création. Une tâche d'avant
    ce champ : ses lecteurs s'il y en a, sinon le jeton unique (vide)."""
    if "officine" in meta:
        return str(meta.get("officine") or "")
    lecteurs = meta.get("lecteurs") or []
    return str(lecteurs[0]) if lecteurs else ""


def _lire(chemin: pathlib.Path) -> list[dict]:
    if not chemin.exists():
        return []
    brut = json.loads(chemin.read_text(encoding="utf-8"))
    return [e for e in (brut.get("jetons") or []) if isinstance(e, dict)]


def _ecrire(chemin: pathlib.Path, entrees: list[dict]) -> None:
    # écrit à côté puis remplace : l'agent qui relit le fichier ne le voit jamais à moitié
    chemin.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=chemin.parent, prefix=".jetons-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"jetons": entrees}, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, chemin)


def creer(chemin, compte: str, nom: str | None = None) -> str:
    """Délivre un jeton pour ce compte client et en garde l'empreinte. Rend le jeton : il ne
    sera plus jamais affiché. Un compte peut avoir plusieurs jetons (un par poste, ou le temps
    de remplacer l'ancien)."""
    if not normal(compte):
        raise ValueError("un jeton se rattache à un numéro de compte client")
    p = pathlib.Path(chemin)
    jeton = PREFIXE + secrets.token_urlsafe(32)
    entrees = _lire(p)
    entrees.append({"compte_client": str(compte).strip(), "nom": nom or None,
                    "empreinte": empreinte(jeton), "cree_le": dt.date.today().isoformat()})
    _ecrire(p, entrees)
    return jeton


def revoquer(chemin, compte: str | None = None, empreinte_debut: str | None = None) -> int:
    """Retire les jetons d'un compte client, ou le seul jeton dont l'empreinte commence ainsi.
    Rend le nombre de jetons retirés."""
    if not compte and not empreinte_debut:
        raise ValueError("dire quel compte, ou quelle empreinte")
    p = pathlib.Path(chemin)
    entrees = _lire(p)
    garder = [e for e in entrees
              if not ((compte and normal(e.get("compte_client")) == normal(compte))
                      or (empreinte_debut and str(e.get("empreinte", "")).startswith(empreinte_debut)))]
    _ecrire(p, garder)
    return len(entrees) - len(garder)


def lister(chemin) -> list[dict]:
    return _lire(pathlib.Path(chemin))


class Registre:
    """Le registre tel que l'agent le lit : relu quand le fichier change. Un fichier illisible
    ne laisse passer aucun jeton d'officine (on ne devine pas)."""

    def __init__(self, chemin):
        self.chemin = pathlib.Path(chemin)
        self._mtime, self._index = None, {}

    def _rafraichir(self):
        try:
            m = self.chemin.stat().st_mtime_ns
        except FileNotFoundError:
            m = None
        if m == self._mtime:
            return
        self._mtime = m
        try:
            entrees = _lire(self.chemin) if m is not None else []
        except Exception as e:
            log.error("registre des jetons illisible (%s) : aucun jeton d'officine accepté", e)
            entrees = []
        self._index = {str(e.get("empreinte")): str(e.get("compte_client") or "")
                       for e in entrees if e.get("empreinte") and normal(e.get("compte_client"))}

    def compte_de(self, jeton: str) -> str | None:
        """Le compte client auquel ce jeton est rattaché, ou None s'il n'est pas au registre."""
        self._rafraichir()
        cherche = empreinte(jeton)
        trouve = None
        for e, compte in self._index.items():
            # comparaison à temps constant, et on parcourt tout : la durée ne dit rien
            if hmac.compare_digest(e.encode(), cherche.encode()):
                trouve = compte
        return trouve
