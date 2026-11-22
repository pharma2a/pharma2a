"""Le canal de démonstration : un fournisseur fictif qui répond tout seul, pour essayer
PharmA2A sans laboratoire en face. PHARMA2A_BACKEND=demo.

Il ne transmet rien à personne. Chaque commande reçoit, à la relecture suivante, un accusé de
réception d'origine « erp » calculé sur un petit stock inventé : les cinq références de la
fiche d'exemple (schema/exemples/fiche_fournisseur.json). Une référence qu'il ne vend pas
revient avec une quantité de 0 et un commentaire, le reste de la commande passe. Une
proposition reçoit le colisage et les conditions, sans numéro de commande.

Tout est en mémoire : un redémarrage oublie les commandes en attente de réponse."""
from __future__ import annotations
import datetime as dt
import uuid
from . import Reponse

# CIP 13 fictifs : le stock et le colisage du fournisseur de démonstration
STOCK = {"3400000000011": 120, "3400000000028": 4, "3400000000035": 0, "3400000000042": 30, "3400000000059": 12}
COLISAGE = {"3400000000011": 6, "3400000000042": 3}
FRANCO_HT, DELAI_JOURS = 400, 3


def accuse(commande: dict) -> dict:
    proposition = commande.get("nature") == "proposition"
    lignes = []
    for l in commande.get("lignes") or []:
        code = l.get("cip13") or l.get("ean13") or l.get("ref_fournisseur") or ""
        base = {"id_ligne": l["id_ligne"], **{k: l[k] for k in ("cip13", "ean13", "ref_fournisseur") if l.get(k)}}
        if code not in STOCK:
            lignes.append({**base, "quantite_confirmee": 0, "rupture": False,
                           "commentaire": "Référence absente du catalogue du fournisseur de démonstration."})
            continue
        if proposition:
            lignes.append({**base, "colisage": COLISAGE.get(code)})
            continue
        part = min(int(l["quantite"]), STOCK[code])
        suit = l.get("reliquat_accepte")
        suit = (commande.get("reliquat_accepte") if suit is None else suit) is not False
        lignes.append({**base, "quantite_confirmee": part, "colisage": COLISAGE.get(code),
                       "reliquat": int(l["quantite"]) - part if part and suit else 0, "rupture": STOCK[code] == 0})
    a = {"version": "1", "reference": commande["reference"], "origine": "erp", "lignes": lignes,
         "conditions": {"franco_ht": FRANCO_HT, "delai_livraison_jours": DELAI_JOURS, "catalogue_joint": None},
         "commentaire": "Fournisseur de démonstration : rien n'est commandé ni livré."}
    if not proposition:
        a["numero_commande_labo"] = "DEMO-" + uuid.uuid4().hex[:6].upper()
        a["date_livraison_prevue"] = (dt.date.today() + dt.timedelta(days=DELAI_JOURS)).isoformat()
    return a


class Backend:
    origine = "erp"

    def __init__(self, config):
        self.c = config
        self._attente: dict[str, Reponse] = {}

    def transmettre(self, message: str, doc: dict, reference: str, message_id: str | None = None) -> str:
        envoi = message_id or f"<{uuid.uuid4()}@pharma2a>"
        if message == "commande":            # l'état de rayon et la relance n'appellent pas de réponse
            ident = f"demo-{uuid.uuid4().hex}"
            self._attente[ident] = Reponse(identifiant=ident, objet="", texte="", recu_le=dt.datetime.now(dt.timezone.utc),
                                           reference=reference, reference_explicite=True, accuse=accuse(doc), en_reponse_a=[envoi])
        return f"demo:{reference}"

    def lire_reponses(self, depuis: dt.datetime | None = None) -> list[Reponse]:
        return list(self._attente.values())

    def acquitter(self, identifiants: list[str]) -> None:
        for i in identifiants:
            self._attente.pop(i, None)
