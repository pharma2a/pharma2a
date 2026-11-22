"""L'essai de conformité : un agent de fournisseur parle-t-il PharmA2A comme la spécification
le demande ? Le programme joue l'officine, pose les mêmes questions à tout agent et rend un
verdict par règle, avec l'article de SPECIFICATION.md.

    python -m pharma2a.essai --agent URL                              la carte, la porte, la fiche : aucune commande ne part
    python -m pharma2a.essai --agent URL --jeton J                    en plus : la fiche demandée avec le jeton
    python -m pharma2a.essai --agent URL --jeton J --compte N --cip C --commander
                                                                      en plus : de vraies commandes d'essai

--commander envoie des commandes à l'agent : à ne lancer que sur un agent d'essai, avec un
compte client d'essai et un produit que le fournisseur vend (--cip, et --ean pour essayer aussi
l'EAN 13). Chaque commande dit « commande d'essai, à ne pas livrer ».

Quatre verdicts : « conforme », « écart » (une règle MUST n'est pas tenue, ou le serveur
plante), « à voir » (une recommandation, ou ce qui ne se vérifie pas de l'extérieur) et
« non vérifié » (l'essai n'a pas pu aller jusque-là). Le code de sortie vaut 1 s'il y a un écart."""
from __future__ import annotations
import argparse
import copy
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from . import officine
from .dictionnaire import EXTENSION, MEDIA, SCHEMAS, valider
from .officine import EN_COURS, accuse_de, origine, propre, texte_etat

CONFORME, ECART, A_VOIR, NON_VERIFIE = "conforme", "écart", "à voir", "non vérifié"
FERMEES = {"TASK_STATE_COMPLETED", "TASK_STATE_REJECTED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED"}
REFUS = {"TASK_STATE_REJECTED", "TASK_STATE_FAILED"}
INCONNU = "3400000000999"        # un code 13 de la série fictive des exemples, qu'aucun fournisseur ne vend
MOT = "Commande d'essai de conformité PharmA2A, à ne pas livrer."
PASSAGE = 8                      # secondes laissées à une tâche pour quitter l'état « reçue »


class Sonde(officine.Agent):
    """L'officine de démonstration, qui rend le code HTTP au lieu de s'arrêter à la première erreur.
    Mêmes gardes : le jeton ne part qu'à l'origine de l'agent, et jamais en clair hors de la machine."""

    def brut(self, methode: str, chemin: str, corps: dict | None = None, *, base: str | None = None,
             jeton: str | None = "le mien") -> tuple[int, dict | None, str]:
        en_tetes = {"Accept": "application/json", "A2A-Version": officine.VERSION, "A2A-Extensions": EXTENSION}
        j = self.jeton if jeton == "le mien" else jeton
        if j:
            en_tetes["Authorization"] = f"Bearer {j}"
        data = None
        if corps is not None:
            data = json.dumps(corps, ensure_ascii=False).encode("utf-8")
            en_tetes["Content-Type"] = "application/json"
        cible = (base or self.base) + chemin
        if (data is not None or j) and (origine(cible) not in self.origines or not officine._sur(cible)):
            return 0, None, "adresse hors de l'origine de l'agent ou sans HTTPS : rien n'y est envoyé"
        req = urllib.request.Request(cible, data=data, headers=en_tetes, method=methode)
        local = origine(cible)[1] in officine._LOCAL or origine(cible)[1].endswith(".localhost")
        try:
            with (officine._OUVREUR_LOCAL if local else officine._OUVREUR).open(req, timeout=self.timeout) as r:
                code, texte = r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            code, texte = e.code, (e.read().decode("utf-8", "replace") if e.fp else "")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            return 0, None, f"pas de réponse ({getattr(e, 'reason', e)})"
        try:
            doc = json.loads(texte) if texte else None
        except ValueError:
            doc = None
        doc = doc if isinstance(doc, dict) else None
        erreur = ((doc or {}).get("error") or {}).get("message") if isinstance((doc or {}).get("error"), dict) else None
        return code, doc, str(erreur or ("" if doc else texte))[:200]

    def message(self, doc: dict, media: str) -> tuple[int, dict | None, str]:
        m = {"messageId": str(uuid.uuid4()), "role": "ROLE_USER", "extensions": [EXTENSION], "parts": [{"data": doc, "mediaType": media}]}
        return self.brut("POST", "/message:send", {"message": m, "configuration": {"returnImmediately": True}})


class Essai:
    def __init__(self, sonde: Sonde, compte: str | None, cip: str | None, ean: str | None, prefixe: str, attendre: int):
        self.s, self.compte, self.cip, self.ean, self.prefixe, self.attendre = sonde, compte, cip, ean, prefixe, attendre
        self.resultats: list[tuple[str, str, str, str]] = []
        self.n = 0

    def note(self, verdict: str, article: str, regle: str, detail: str = "") -> None:
        self.resultats.append((verdict, article, regle, propre(detail)))

    # ---------------------------------------------------------------- sans rien envoyer
    def carte(self) -> bool:
        code, c, texte = self.s.brut("GET", "/.well-known/agent-card.json", base=self.s.url, jeton=None)
        if code != 200 or c is None:
            self.note(ECART, "3.1", "La carte de l'agent se lit sans jeton", f"HTTP {code} {texte}")
            return False
        self.note(CONFORME, "3.1", "La carte de l'agent se lit sans jeton", str(c.get("name") or ""))
        exts = [e.get("uri") for e in (c.get("capabilities") or {}).get("extensions") or []]
        self.note(CONFORME if EXTENSION in exts else A_VOIR, "3.2", "La carte déclare l'extension PharmA2A",
                  "" if EXTENSION in exts else "extension absente de capabilities.extensions")
        ifaces = [i for i in c.get("supportedInterfaces") or [] if i.get("protocolBinding") == "HTTP+JSON" and i.get("url")]
        if not ifaces:
            self.note(A_VOIR, "3.1", "Une interface HTTP+JSON est annoncée", "cet essai ne parle que HTTP+JSON : la suite peut échouer")
        for i in ifaces[:1]:
            ok = officine._sur(i["url"]) and origine(i["url"]) in self.s.origines
            self.note(CONFORME if ok else ECART, "11.1", "L'adresse annoncée est en HTTPS, à l'origine de l'agent",
                      "" if ok else f"la carte annonce {i['url']} : le jeton et la commande n'y partiraient pas en sûreté")
        try:
            self.s.carte()                 # retient l'adresse des opérations, tenant compris
        except SystemExit as e:
            self.note(A_VOIR, "3.1", "La carte donne l'adresse des opérations", str(e))
        return True

    def porte(self) -> None:
        vide = {"message": {"messageId": str(uuid.uuid4()), "role": "ROLE_USER", "parts": [{"text": "essai"}]}}
        for nom, methode, chemin, corps, jeton in (
                ("Sans jeton, l'envoi d'un message est refusé", "POST", "/message:send", vide, None),
                ("Sans jeton, la lecture d'une tâche est refusée", "GET", f"/tasks/{uuid.uuid4()}", None, None),
                ("Avec un jeton inventé, la lecture d'une tâche est refusée", "GET", f"/tasks/{uuid.uuid4()}", None, "p2a_jeton_invente_pour_l_essai")):
            code, _, texte = self.s.brut(methode, chemin, corps, jeton=jeton)
            if code == 0:
                self.note(NON_VERIFIE, "11.1", nom, texte)
            else:
                self.note(CONFORME if code in (401, 403) else ECART, "11.1", nom, f"HTTP {code}" + ("" if code in (401, 403) else f" {texte}"))

    def fiche(self) -> None:
        code, f, texte = self.s.brut("GET", "/fiche_fournisseur.json", base=self.s.url, jeton=None)
        if code != 200 or f is None:
            self.note(A_VOIR, "6.1", "La fiche du fournisseur se lit sans jeton", f"HTTP {code} : la spécification la recommande")
        else:
            err = valider("fiche_fournisseur", f)
            self.note(CONFORME if not err else ECART, "6.1", "La fiche du fournisseur se lit sans jeton et suit le dictionnaire", "; ".join(err[:3]))
            tarif = any(l.get("prix_tarif_ht") is not None for l in f.get("gamme") or [] if isinstance(l, dict))
            self.note(A_VOIR if tarif else CONFORME, "6.2", "Sans jeton, le tarif catalogue est masqué",
                      "tarif visible : permis seulement si le fournisseur l'a choisi" if tarif else "")
        if not self.s.jeton:
            self.note(NON_VERIFIE, "6.1", "La fiche est rendue à une demande avec jeton", "pas de jeton donné")
            return
        code, r, texte = self.s.message({"version": "1", "demande": "fiche_fournisseur"}, MEDIA["fiche_fournisseur"])
        t = (r or {}).get("task") or {}
        if t:
            t = self._suivre(t, 0)
        rendue = next((p.get("data") for a in t.get("artifacts") or [] for p in a.get("parts") or []
                       if p.get("mediaType") == MEDIA["fiche_fournisseur"] and isinstance(p.get("data"), dict)), None)
        if rendue is not None:
            err = valider("fiche_fournisseur", rendue)
            self.note(CONFORME if not err else ECART, "6.1", "La fiche est rendue à une demande avec jeton", "; ".join(err[:3]))
        else:
            self.note(ECART, "6.1", "La fiche est rendue à une demande avec jeton",
                      f"HTTP {code}, {(t.get('status') or {}).get('state') or 'pas de tâche'} {texte_etat(t) if t else texte}")

    # ---------------------------------------------------------------- avec de vraies commandes d'essai
    def _commande(self, lignes: list[dict], **plus) -> dict:
        self.n += 1
        c = json.loads((SCHEMAS / "exemples" / "commande.json").read_text(encoding="utf-8"))
        c.update(reference=f"{self.prefixe}-{self.n}", date=time.strftime("%Y-%m-%d"), message=MOT,
                 lignes=[{"id_ligne": str(i + 1), "libelle": "PRODUIT D'ESSAI", "quantite": 1, "colisage": None, **l} for i, l in enumerate(lignes)])
        c["officine"]["numero_compte_client"] = self.compte
        c.update(plus)
        erreurs = valider("commande", c)
        if erreurs:
            raise SystemExit("la commande d'essai ne suit pas le dictionnaire, rien n'est envoyé : " + "; ".join(erreurs[:3]))
        return c

    def _envoyer(self, commande: dict, attendre: bool = True) -> tuple[int, dict | None, str]:
        """Rend (code HTTP, tâche ou None, texte). Une réponse par message direct compte comme un refus expliqué."""
        code, r, texte = self.s.message(commande, MEDIA["commande"])
        t = (r or {}).get("task")
        if not isinstance(t, dict):
            parts = ((r or {}).get("message") or {}).get("parts") or []
            return code, None, " ".join(p.get("text", "") for p in parts if p.get("text")).strip() or texte
        t = self._suivre(t, self.attendre if attendre else 0)
        return code, t, texte_etat(t)

    def _suivre(self, t: dict, attendre: float) -> dict:
        """Relit la tâche. « Reçue » (SUBMITTED) n'est qu'un passage : on attend toujours qu'elle le quitte
        (au plus PASSAGE secondes), puis la réponse du fournisseur au plus `attendre` secondes."""
        debut = time.monotonic()
        while (t["status"]["state"] == "TASK_STATE_SUBMITTED" and time.monotonic() - debut < PASSAGE) or \
                (t["status"]["state"] in EN_COURS and time.monotonic() - debut < attendre):
            time.sleep(0.5)
            code, t2, _ = self.s.brut("GET", f"/tasks/{urllib.parse.quote(t['id'], safe='')}")
            if code == 200 and t2:
                t = t2
        return t

    def _accuse(self, commande: dict, t: dict, quoi: str) -> None:
        a = accuse_de(t)
        if a is None:
            self.note(NON_VERIFIE, "9", f"{quoi} : l'accusé de réception", f"pas encore d'accusé ({t['status']['state']}) : relancer avec --attendre")
            return
        err = valider("accuse_reception", a)
        ids = [str(l.get("id_ligne")) for l in a.get("lignes") or []]
        connues = {str(l["id_ligne"]): l for l in commande["lignes"]}
        if a.get("reference") != commande["reference"]:
            err.append("la référence n'est pas celle de la commande")
        if len(ids) != len(set(ids)) or any(i not in connues for i in ids):
            err.append("id_ligne répété ou absent de la commande")
        for l in a.get("lignes") or []:
            d = connues.get(str(l.get("id_ligne"))) or {}
            if any(l.get(k) and l[k] != d.get(k) for k in ("cip13", "ean13", "ref_fournisseur")):
                err.append(f"ligne {l.get('id_ligne')} : un code produit qui n'est pas celui de la ligne commandée")
        self.note(CONFORME if not err else ECART, "9.1, 9.3", f"{quoi} : l'accusé suit le dictionnaire et répond à la commande", "; ".join(err[:3]))

    def _ouverte(self, quoi: str, commande: dict, attendre: bool = True) -> dict | None:
        code, t, texte = self._envoyer(commande, attendre=attendre)
        if code >= 500 or code == 0:
            self.note(ECART, "7.1", f"{quoi} : la commande ouvre une tâche", f"HTTP {code} : le serveur plante ou ne répond pas")
        elif t is None or t["status"]["state"] in REFUS:
            self.note(ECART, "7.1", f"{quoi} : la commande ouvre une tâche", f"HTTP {code}, {t['status']['state'] if t else 'pas de tâche'} : {texte}")
        else:
            self.note(CONFORME, "7.1", f"{quoi} : la commande ouvre une tâche", f"{t['status']['state']} : {texte}")
            if attendre:
                self._accuse(commande, t, quoi)
            return t
        return None

    def commandes(self) -> None:
        cip = {"cip13": self.cip}
        premiere = self._commande([cip])
        # le renvoi s'essaie tout de suite, tant que la tâche est ouverte ; l'accusé s'attend ensuite
        t = self._ouverte("Une ligne par CIP 13", premiere, attendre=False)
        if t is not None and t["status"]["state"] not in FERMEES:
            _, t2, _ = self._envoyer(premiere, attendre=False)
            self.note(CONFORME if t2 and t2["id"] == t["id"] else ECART, "8.1", "La même référence renvoyée rend la tâche déjà ouverte",
                      "" if t2 and t2["id"] == t["id"] else "une autre tâche, ou aucune : la commande partirait deux fois")
            autre = copy.deepcopy(premiere)
            autre["lignes"][0]["quantite"] = 2
            code, t3, texte = self._envoyer(autre, attendre=False)
            refuse = code in range(400, 500) or t3 is None or (t3["id"] != t["id"] and t3["status"]["state"] == "TASK_STATE_REJECTED")
            self.note(CONFORME if refuse else ECART, "8.2", "La même référence avec un autre contenu est refusée, tâche ouverte", texte)
        elif t is not None:
            self.note(NON_VERIFIE, "8.1, 8.2", "Le renvoi d'une référence dont la tâche est ouverte", "la tâche s'est close aussitôt : rien à renvoyer")
        if t is not None:
            self._accuse(premiere, self._suivre(t, self.attendre), "Une ligne par CIP 13")
        if self.ean:
            self._ouverte("Une ligne par EAN 13", self._commande([{"ean13": self.ean}]))
            if self.ean != self.cip:
                self._ouverte("Deux lignes, CIP et EAN", self._commande([cip, {"ean13": self.ean}]))
        else:
            self.note(NON_VERIFIE, "4.6", "Une ligne par EAN 13", "pas de --ean donné")

        melee = self._commande([cip, {"cip13": INCONNU}])
        code, t, texte = self._envoyer(melee)
        if code >= 500 or code == 0:
            self.note(ECART, "7.1", "Un produit inconnu ne fait pas planter le serveur", f"HTTP {code}")
        elif t is None or t["status"]["state"] == "TASK_STATE_REJECTED":
            self.note(ECART, "7.1", "Un produit inconnu se dit sur sa ligne, le reste de la commande passe",
                      f"toute la commande est rejetée ({texte}) : le rejet est réservé au message hors dictionnaire et au doublon")
        else:
            a = accuse_de(t)
            l2 = next((l for l in (a or {}).get("lignes") or [] if str(l.get("id_ligne")) == "2"), None)
            if l2 is None:
                self.note(NON_VERIFIE, "7.1", "Un produit inconnu se dit sur sa ligne", f"pas encore d'accusé pour la ligne ({t['status']['state']})")
            else:
                ok = not l2.get("quantite_confirmee") and t["status"]["state"] != "TASK_STATE_COMPLETED"
                self.note(CONFORME if ok else ECART, "7.1", "Un produit inconnu se dit sur sa ligne et revient au pharmacien",
                          "" if ok else f"ligne confirmée ou tâche terminée ({t['status']['state']}) alors qu'un produit manque")

        hors = self._commande([cip])
        hors["lignes"][0]["prix_ht"] = 9.9
        code, t, texte = self._envoyer(hors, attendre=False)
        refuse = code in range(400, 500) or t is None or t["status"]["state"] == "TASK_STATE_REJECTED"
        self.note(ECART if code >= 500 or not refuse else CONFORME, "4.3", "Une commande hors dictionnaire (un prix sur une ligne) est rejetée",
                  f"HTTP {code}, {t['status']['state'] if t else 'pas de tâche'} : {texte}")

        ailleurs = self._commande([cip])
        ailleurs["officine"]["numero_compte_client"] = "ESSAI-AUTRE-COMPTE"
        code, t, texte = self._envoyer(ailleurs, attendre=False)
        refuse = code in range(400, 500) or t is None or t["status"]["state"] in REFUS
        self.note(CONFORME if refuse and code < 500 else (ECART if code >= 500 else A_VOIR), "11.2", "Une commande pour un autre compte client est refusée",
                  texte if refuse else "acceptée : le jeton n'est pas lié à un compte (11.1 recommande de le lier)")

        devis = self._commande([cip], nature="proposition", questions=["franco", "colisage"])
        code, t, texte = self._envoyer(devis, attendre=False)
        self.note(ECART if code >= 500 or code == 0 else A_VOIR, "7.3", "Une proposition reçoit une réponse, sans passer de commande",
                  f"HTTP {code}, {t['status']['state'] if t else 'pas de tâche'} : {texte} (qu'aucune commande n'est créée se vérifie chez le fournisseur)")

    def passer(self, commander: bool) -> None:
        if not self.carte():
            return
        self.porte()
        self.fiche()
        if commander:
            self.commandes()
        else:
            self.note(NON_VERIFIE, "7 à 9", "Les commandes, l'idempotence et les accusés", "relancer avec --commander sur un agent d'essai")


def rapport(resultats: list[tuple[str, str, str, str]]) -> str:
    lignes = [f"{v:<12}{'§ ' + a:<12}{r}" + (f"\n{'':<24}{d}" if d else "") for v, a, r, d in resultats]
    compte = {v: sum(1 for x in resultats if x[0] == v) for v in (CONFORME, ECART, A_VOIR, NON_VERIFIE)}
    return "\n".join(lignes) + "\n\n" + " · ".join(f"{n} {v}" for v, n in compte.items())


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m pharma2a.essai", description=__doc__.split("\n\n")[0])
    p.add_argument("--agent", default=os.environ.get("PHARMA2A_URL") or "http://127.0.0.1:8765", help="l'adresse de l'agent du fournisseur")
    p.add_argument("--jeton", default=os.environ.get("PHARMA2A_JETON"), help="le jeton d'essai délivré par le fournisseur (défaut : PHARMA2A_JETON)")
    p.add_argument("--compte", help="le numéro de compte client d'essai auquel le jeton est lié")
    p.add_argument("--cip", help="le CIP 13 d'un produit que le fournisseur vend")
    p.add_argument("--ean", help="l'EAN 13 d'un produit que le fournisseur vend, pour essayer aussi ce champ")
    p.add_argument("--commander", action="store_true", help="envoyer de vraies commandes d'essai (agent d'essai seulement)")
    p.add_argument("--prefixe", default=time.strftime("ESSAI-%Y%m%d-%H%M%S"), help="le début des références des commandes d'essai")
    p.add_argument("--attendre", type=int, default=0, help="attendre l'accusé de chaque commande, au plus ce nombre de secondes")
    p.add_argument("--autoriser", action="append", default=[], metavar="URL", help="une autre origine où la carte annonce ses opérations")
    p.add_argument("--json", action="store_true", help="rendre le résultat en JSON")
    a = p.parse_args(argv)
    if a.commander and not (a.jeton and a.compte and a.cip):
        print("--commander demande --jeton, --compte et --cip : rien n'est envoyé", file=sys.stderr)
        return 2
    essai = Essai(Sonde(a.agent, a.jeton, autorisees=a.autoriser), a.compte, a.cip, a.ean, a.prefixe, a.attendre)
    essai.passer(a.commander)
    if a.json:
        print(json.dumps([{"verdict": v, "article": x, "regle": r, "detail": d} for v, x, r, d in essai.resultats], ensure_ascii=False, indent=1))
    else:
        print(rapport(essai.resultats))
    return 1 if any(v == ECART for v, *_ in essai.resultats) else 0


if __name__ == "__main__":
    sys.exit(main())
