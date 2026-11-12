"""La fiche du laboratoire : le guichet. Qui il est, comment on le joint, ses conditions
générales, sa gamme. Lisible sans clé sur /fiche_fournisseur.json, comme une vitrine, et
rendue en artefact à l'agent d'une officine qui la demande par A2A.

Le fichier (PHARMA2A_FICHE, JSON) est relu quand il change. Une fiche qui ne respecte pas
le dictionnaire n'est pas servie : l'agent le dit, il ne sert pas une fiche à moitié."""
from __future__ import annotations
import hashlib
import json
import logging
import pathlib
from email.utils import formatdate
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from .dictionnaire import valider

log = logging.getLogger("pharma2a.fiche")
FICHE = "/fiche_fournisseur.json"
DEMANDE = "fiche_fournisseur"      # ce que l'officine met dans « demande » pour l'obtenir par A2A


class Fiche:
    """Le fichier de la fiche, relu quand il change, validé à chaque lecture."""

    def __init__(self, chemin: str | pathlib.Path | None):
        self.chemin = pathlib.Path(chemin) if chemin else None
        self._mtime, self._doc, self._erreur = None, None, None

    def _rafraichir(self):
        if self.chemin is None:
            return
        try:
            m = self.chemin.stat().st_mtime
        except FileNotFoundError:
            m = None
        if m == self._mtime:
            return
        self._mtime, self._doc, self._erreur = m, None, None
        if m is None:
            self._erreur = "le fichier de la fiche n'existe pas"
            return
        try:
            doc = json.loads(self.chemin.read_text(encoding="utf-8"))
        except Exception as e:
            self._erreur = f"fiche illisible : {e}"
            log.error(self._erreur)
            return
        erreurs = valider("fiche_fournisseur", doc)
        if erreurs:
            self._erreur = "fiche non conforme au dictionnaire : " + " ; ".join(erreurs[:8])
            log.error(self._erreur)
            return
        self._doc = doc
        log.info("fiche du fournisseur relue : %s référence(s)", len(doc.get("gamme") or []))

    def lire(self) -> tuple[dict | None, str | None]:
        """(la fiche, None) si elle est servable ; (None, pourquoi) sinon."""
        if self.chemin is None:
            return None, "le fournisseur n'a pas publié sa fiche"
        self._rafraichir()
        return self._doc, self._erreur


def sans_tarif(doc: dict) -> dict:
    """La fiche sans le tarif : ce que la vitrine montre quand le fournisseur ne publie pas ses prix.
    Le tarif part sous toutes ses formes : le bloc, le prix par référence, et le document tarifaire."""
    out = {k: v for k, v in doc.items() if k != "tarif"}
    out["gamme"] = [{k: v for k, v in l.items() if k != "prix_tarif_ht"} for l in doc.get("gamme") or []]
    if "documents" in doc:
        out["documents"] = [d for d in doc.get("documents") or [] if d.get("nature") != "tarif"]
    return out


# ce que la vitrine dit d'une fiche qu'elle ne sert pas : le détail de validation peut citer une
# valeur du tarif, il reste dans le journal du fournisseur et dans la réponse A2A authentifiée
INDISPONIBLE = "fiche indisponible : le fournisseur doit la corriger (détail dans son journal)"


def route_fiche(fiche: Fiche, cache_control: str = "public, max-age=3600", tarif_public: bool = False) -> Route:
    async def servir(request: Request) -> Response:
        doc, pourquoi = fiche.lire()
        if doc is None:
            if fiche.chemin is None:
                return JSONResponse({"erreur": pourquoi}, status_code=404)
            return JSONResponse({"erreur": INDISPONIBLE}, status_code=503)
        if not tarif_public:
            doc = sans_tarif(doc)
        corps = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        etag = '"' + hashlib.sha256(corps).hexdigest()[:32] + '"'
        en_tetes = {"ETag": etag, "Cache-Control": cache_control,
                    "Last-Modified": formatdate(fiche._mtime, usegmt=True)}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=en_tetes)
        if request.method == "HEAD":
            return Response(status_code=200, headers=en_tetes, media_type="application/json")
        return Response(corps, headers=en_tetes, media_type="application/json")

    return Route(FICHE, servir, methods=["GET", "HEAD"])
