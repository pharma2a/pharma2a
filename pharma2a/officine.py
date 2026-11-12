"""La pharmacie de démonstration : une officine en ligne de commande, pour essayer un agent de
fournisseur PharmA2A, celui de ce dépôt ou le sien. Elle lit la carte, envoie une commande du
dictionnaire, puis relit la tâche et montre l'accusé de réception. Ce n'est pas un agent
d'achat : elle ne décide rien, elle envoie ce qu'on lui donne.

    python -m pharma2a.officine                         la commande d'exemple, à l'agent local
    python -m pharma2a.officine --agent URL --jeton J   à un autre agent
    python -m pharma2a.officine --commande ma.json      une autre commande
    python -m pharma2a.officine --tache ID              relire une tâche déjà envoyée
    python -m pharma2a.officine --attendre 600          relire jusqu'à la réponse (secondes)
    python -m pharma2a.officine --autoriser URL         accepter que la carte désigne une autre origine

Le jeton ne part qu'à l'adresse de l'agent : aucune redirection vers une autre origine n'est
suivie, et une carte qui annonce ses opérations ailleurs est ignorée sans --autoriser.

Bibliothèque standard seule pour le transport : HTTP+JSON d'A2A 1.0."""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from .dictionnaire import EXTENSION, MEDIA, SCHEMAS, valider

EN_COURS = {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"}
VERSION = "1.0"      # la version d'A2A que nos requêtes annoncent


def origine(url: str) -> tuple[str, str, int | None]:
    u = urllib.parse.urlsplit(url)
    return u.scheme, (u.hostname or "").lower(), u.port or {"http": 80, "https": 443}.get(u.scheme)


class _MemeOrigine(urllib.request.HTTPRedirectHandler):
    """Aucune redirection vers une autre origine n'est suivie, pas même pour la carte publique :
    urllib y recopierait le jeton, et une carte venue d'ailleurs pourrait désigner où envoyer
    le jeton et la commande."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if origine(newurl) != origine(req.full_url):
            raise urllib.error.HTTPError(req.full_url, code, f"redirection refusée vers une autre origine : {newurl}", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OUVREUR = urllib.request.build_opener(_MemeOrigine)
# un appel vers la machine elle-même ne passe jamais par un proxy (http_proxy) : il y
# porterait le jeton et la commande en clair
_OUVREUR_LOCAL = urllib.request.build_opener(_MemeOrigine, urllib.request.ProxyHandler({}))
_LOCAL = {"localhost", "127.0.0.1", "::1"}


def _sur(url: str) -> bool:
    """HTTPS partout ; HTTP seulement vers la machine elle-même (un agent lancé pour essayer)."""
    sch, hote, _ = origine(url)
    return sch == "https" or (sch == "http" and (hote in _LOCAL or hote.endswith(".localhost")))


class Agent:
    def __init__(self, url: str, jeton: str | None, timeout: int = 30, autorisees: list[str] | None = None):
        self.url, self.jeton, self.timeout = url.rstrip("/"), jeton, timeout
        self.base = self.url        # où vont les opérations : l'interface HTTP+JSON que la carte annonce
        # le jeton ne va qu'à l'origine configurée, ou à celles que l'utilisateur autorise lui-même
        self.origines = {origine(self.url)} | {origine(u) for u in autorisees or []}

    def appel(self, methode: str, chemin: str, corps: dict | None = None, *, base: str | None = None,
              authentifie: bool = True) -> dict:
        en_tetes = {"Accept": "application/json", "A2A-Version": VERSION, "A2A-Extensions": EXTENSION}
        if self.jeton and authentifie:
            en_tetes["Authorization"] = f"Bearer {self.jeton}"
        data = None
        if corps is not None:
            data = json.dumps(corps, ensure_ascii=False).encode("utf-8")
            en_tetes["Content-Type"] = "application/json"
        cible = (base or self.base) + chemin
        if "Authorization" in en_tetes and origine(cible) not in self.origines:
            raise SystemExit(f"{cible} n'est pas à l'adresse de l'agent : le jeton n'y part pas (--autoriser pour l'accepter)")
        # le jeton et la commande ne voyagent jamais en clair hors de la machine (A2A 1.0, 7.1)
        if (data is not None or "Authorization" in en_tetes) and not _sur(cible):
            raise SystemExit(f"{cible} n'est pas en HTTPS : le jeton et la commande n'y partent pas (HTTP seulement en local)")
        req = urllib.request.Request(cible, data=data, headers=en_tetes, method=methode)
        try:
            ouvreur = _OUVREUR_LOCAL if origine(cible)[1] in _LOCAL or origine(cible)[1].endswith(".localhost") else _OUVREUR
            with ouvreur.open(req, timeout=self.timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            corps_err = (e.read().decode("utf-8", "replace")[:300] if e.fp else "") or e.msg
            raise SystemExit(f"{methode} {chemin} : HTTP {e.code} {propre(corps_err)}")
        except urllib.error.URLError as e:
            raise SystemExit(f"{self.url} ne répond pas ({e.reason}) : l'agent est-il lancé ?")

    def carte(self) -> dict:
        """La carte est publique : elle se lit sans jeton. On en retient l'interface HTTP+JSON de la
        version que nos requêtes annoncent (1.0), quel que soit l'ordre de la liste, avec son
        tenant : en HTTP+JSON il précède le chemin (/{tenant}/message:send)."""
        c = self.appel("GET", "/.well-known/agent-card.json", base=self.url, authentifie=False)
        for i in c.get("supportedInterfaces") or []:
            v = str(i.get("protocolVersion", ""))
            if i.get("protocolBinding") != "HTTP+JSON" or not i.get("url") or not (v == VERSION or v.startswith(VERSION + ".")):
                continue
            if origine(i["url"]) not in self.origines:
                print(f"la carte annonce ses opérations sur une autre origine ({propre(i['url'])}) : ignorée, "
                      "--autoriser pour l'accepter", file=sys.stderr)
                continue
            self.base = i["url"].rstrip("/") + (("/" + urllib.parse.quote(str(i["tenant"]), safe="")) if i.get("tenant") else "")
            break
        return c

    def envoyer(self, commande: dict) -> dict:
        """Rend la tâche ; ou, si l'agent répond d'un message direct (un refus expliqué, A2A 1.0
        section 3.1.1), {"message": ...}. La réponse revient tout de suite (returnImmediately) :
        un agent qui attendrait la confirmation du fournisseur ne fait pas expirer la requête,
        le suivi passe par --attendre."""
        message = {"messageId": str(uuid.uuid4()), "role": "ROLE_USER", "extensions": [EXTENSION],
                   "parts": [{"data": commande, "mediaType": MEDIA["commande"]}]}
        r = self.appel("POST", "/message:send", {"message": message, "configuration": {"returnImmediately": True}})
        if isinstance(r.get("task"), dict):
            return r["task"]
        if isinstance(r.get("message"), dict):
            return {"message": r["message"]}
        raise SystemExit(f"réponse inattendue de l'agent : {propre(json.dumps(r, ensure_ascii=False)[:300])}")

    def tache(self, ident: str) -> dict:
        return self.appel("GET", f"/tasks/{urllib.parse.quote(ident, safe='')}")


# les caractères de contrôle d'un texte venu du fournisseur ne vont pas au terminal tels quels
_CONTROLE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def propre(v) -> str:
    return _CONTROLE.sub("?", str(v))


def texte_etat(t: dict) -> str:
    parts = ((t.get("status") or {}).get("message") or {}).get("parts") or []
    return " ".join(p.get("text", "") for p in parts if p.get("text")).strip()


def accuse_de(t: dict) -> dict | None:
    for art in reversed(t.get("artifacts") or []):
        for p in art.get("parts") or []:
            if p.get("mediaType") == MEDIA["accuse_reception"] and isinstance(p.get("data"), dict):
                return p["data"]
    return None


def n(v):
    """Le transport A2A rend les entiers en flottants (6 devient 6.0) : on les montre entiers."""
    return int(v) if isinstance(v, float) and v.is_integer() else v


def montrer(t: dict) -> None:
    print(f"tâche {propre(t['id'])} : {propre(t['status']['state'])}")
    if texte_etat(t):
        print(f"  {propre(texte_etat(t))}")
    a = accuse_de(t)
    if not a:
        return
    print(f"accusé de réception ({propre(a.get('origine'))}) : commande {propre(a.get('numero_commande_labo') or '-')}"
          f" chez le fournisseur, livraison prévue {propre(a.get('date_livraison_prevue') or '-')}")
    for l in a.get("lignes") or []:
        code = l.get("cip13") or l.get("ean13") or l.get("ref_fournisseur") or "-"
        etat = ("rupture" if l.get("rupture") else
                f"{n(l.get('quantite_confirmee'))} confirmé(s)" + (f", {n(l['reliquat'])} en reliquat" if l.get("reliquat") else ""))
        print(propre(f"  ligne {l.get('id_ligne')}  {code}  {etat}") + (f"  « {propre(l['commentaire'])} »" if l.get("commentaire") else ""))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m pharma2a.officine", description=__doc__.split("\n\n")[0])
    p.add_argument("--agent", default=os.environ.get("PHARMA2A_URL") or "http://127.0.0.1:8765", help="l'adresse de l'agent du fournisseur")
    p.add_argument("--jeton", default=os.environ.get("PHARMA2A_JETON"), help="le jeton que le fournisseur a délivré à l'officine (défaut : PHARMA2A_JETON)")
    p.add_argument("--commande", help="un fichier JSON du dictionnaire (défaut : la commande d'exemple)")
    p.add_argument("--reference", help="la référence de la commande (défaut : DEMO- suivi d'un horodatage, pour ne pas répéter une commande déjà reçue)")
    p.add_argument("--tache", help="relire une tâche déjà envoyée, sans rien envoyer")
    p.add_argument("--autoriser", action="append", default=[], metavar="URL",
                   help="une autre origine à laquelle le jeton peut partir, si la carte y annonce ses opérations")
    p.add_argument("--attendre", type=int, default=0, help="relire la tâche jusqu'à la réponse du fournisseur, au plus ce nombre de secondes")
    a = p.parse_args(argv)
    agent = Agent(a.agent, a.jeton, autorisees=a.autoriser)
    if a.tache:
        agent.carte()                     # pour l'adresse des opérations ; la carte se lit sans jeton
        t = agent.tache(a.tache)
    else:
        carte = agent.carte()
        exts = [e.get("uri") for e in (carte.get("capabilities") or {}).get("extensions") or []]
        print(f"agent : {propre(carte.get('name'))}" + ("  (extension PharmA2A déclarée)" if EXTENSION in exts else "  (extension PharmA2A non déclarée)"))
        chemin = a.commande or SCHEMAS / "exemples" / "commande.json"
        commande = json.loads(open(chemin, encoding="utf-8").read())
        if not isinstance(commande, dict):
            print("la commande doit être un objet JSON du dictionnaire, rien n'est envoyé", file=sys.stderr)
            return 2
        if not a.commande or a.reference:
            commande["reference"] = a.reference or time.strftime("DEMO-%Y%m%d-%H%M%S")
        erreurs = valider("commande", commande)
        if erreurs:
            print("la commande ne respecte pas le dictionnaire, rien n'est envoyé :", *erreurs, sep="\n  ", file=sys.stderr)
            return 2
        t = agent.envoyer(commande)
        if "message" in t:
            texte = " ".join(p.get("text", "") for p in t["message"].get("parts") or [] if p.get("text")).strip()
            print(f"l'agent a répondu sans ouvrir de tâche : {propre(texte) or '(message sans texte)'}")
            return 1
        print(f"commande {commande['reference']} envoyée : {len(commande['lignes'])} ligne(s)")
    fin = time.monotonic() + a.attendre
    while t["status"]["state"] in EN_COURS and time.monotonic() < fin:
        time.sleep(min(5, max(0.0, fin - time.monotonic())))
        t = agent.tache(t["id"])
    montrer(t)
    return 0


if __name__ == "__main__":
    sys.exit(main())
