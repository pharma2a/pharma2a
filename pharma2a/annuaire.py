"""L'annuaire : plusieurs agents dans un seul processus, un par laboratoire (ou par couple
officine et laboratoire quand c'est SyderaAI qui héberge le pont en attendant que le
laboratoire lance le sien). Chaque agent vit sous son propre chemin, avec son jeton, son
destinataire et sa copie ; le reste (courrier, magasin, relecture) est partagé.

Le fichier (PHARMA2A_ANNUAIRE, JSON) est relu quand il change : un agent ajouté est servi
sans redémarrage. Exemple :
{
  "agents": [
    {"id": "labo-un-exemple", "fournisseur": "LABO UN", "jetons": "jetons/labo-un.json",
     "destinataire": "commandes@labo.example", "copie": "delegue@labo.example",
     "fiche": "fiches/labo-un.json"}
  ]
}
« jetons » est le registre des jetons des officines de cet agent (voir jetons.py) ; à sa
place, « jeton » (et « compte_client ») pour un agent qui ne sert qu'une officine.
« fiche » est facultatif : le chemin du fichier de la fiche du fournisseur, servie sans clé.
Un groupe de laboratoires fait tourner ses marques de la même façon, sur son propre serveur."""
from __future__ import annotations
import dataclasses
import json
import logging
import pathlib
import re
from starlette.responses import JSONResponse

log = logging.getLogger("pharma2a.annuaire")
ID_VALIDE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")


def charger_fichier(chemin) -> dict[str, dict]:
    p = pathlib.Path(chemin)
    if not p.exists():
        return {}
    try:
        brut = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.error("annuaire illisible (%s) : aucun agent servi", e)
        return {}
    # Un annuaire mal formé ne sert personne : ni les nouveaux agents, ni
    # les anciens ; il le dit, et il ne lève rien, pour que l'ancien contenu ne survive pas
    if not isinstance(brut, dict) or not isinstance(brut.get("agents") or [], list):
        log.error("annuaire mal formé (attendu {\"agents\": [...]}) : aucun agent servi")
        return {}
    out = {}
    for a in brut.get("agents") or []:
        if not isinstance(a, dict):
            log.warning("agent ignoré dans l'annuaire (pas un objet) : %r", a)
            continue
        i = str(a.get("id") or "")
        if a.get("jeton") and a.get("jetons"):
            log.warning("agent ignoré dans l'annuaire (un registre ou un jeton unique, pas les deux) : %r", i)
            continue
        if not ID_VALIDE.match(i) or not (a.get("jeton") or a.get("jetons")):
            log.warning("agent ignoré dans l'annuaire (id ou jeton invalide) : %r", i)
            continue
        out[i] = a
    return out


def config_de(config, entree: dict, identifiant: str):
    """La configuration d'un agent de l'annuaire : la globale, surchargée par l'entrée."""
    champs = {"jeton": entree.get("jeton") or "", "jetons": entree.get("jetons") or "",
              "compte_client": entree.get("compte_client") or "", "agent": identifiant,
              # « laboratoire » : l'ancien nom de la clé, encore lu
              "fournisseur": entree.get("fournisseur") or entree.get("laboratoire") or config.fournisseur,
              "destinataire": entree.get("destinataire") or config.destinataire,
              "copie": entree.get("copie") or config.copie,
              "fiche": entree.get("fiche") or "",
              "url": f"{config.url.rstrip('/')}/{identifiant}"}
    if entree.get("site"):
        champs["site"] = str(entree["site"])
    if entree.get("tout_public") is not None:
        champs["tout_public"] = bool(entree["tout_public"])
    return dataclasses.replace(config, **champs)


class Annuaire:
    """ASGI : /<id>/... vers l'agent <id>, bâti à la demande et rebâti si le fichier change."""

    def __init__(self, config, fabrique, chemin):
        self.config, self.fabrique, self.chemin = config, fabrique, pathlib.Path(chemin)
        self._mtime, self._entrees, self._apps = None, {}, {}

    def _rafraichir(self):
        try:
            m = self.chemin.stat().st_mtime
        except FileNotFoundError:
            m = None
        if m != self._mtime:
            entrees = charger_fichier(self.chemin)
            # le contenu, sa date et les applications changent ensemble
            self._mtime, self._entrees, self._apps = m, entrees, {}          # tout est rebâti au prochain appel (les jetons ont pu changer)
            log.info("annuaire relu : %s agent(s)", len(self._entrees))

    def agents(self) -> dict[str, dict]:
        self._rafraichir()
        return dict(self._entrees)

    def app_pour(self, identifiant: str):
        self._rafraichir()
        e = self._entrees.get(identifiant)
        if not e:
            return None
        if identifiant not in self._apps:
            self._apps[identifiant] = self.fabrique(config_de(self.config, e, identifiant))
        return self._apps[identifiant]

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            # le cycle de vie est porté par l'application racine ; ici rien à faire
            return await self._lifespan(receive, send)
        if scope["type"] != "http":
            return
        chemin = scope.get("path") or "/"
        morceaux = chemin.strip("/").split("/", 1)
        app = self.app_pour(morceaux[0]) if morceaux and morceaux[0] else None
        if app is None:
            # sans la liste des agents : leurs identifiants peuvent dire quelle officine commande à qui
            rep = JSONResponse({"erreur": "agent inconnu"}, status_code=404)
            return await rep(scope, receive, send)
        reste = "/" + (morceaux[1] if len(morceaux) > 1 else "")
        sous = dict(scope, path=reste, root_path=(scope.get("root_path") or "") + "/" + morceaux[0], raw_path=reste.encode())
        return await app(sous, receive, send)

    @staticmethod
    async def _lifespan(receive, send):
        while True:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif msg["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
