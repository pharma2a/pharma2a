"""La relecture : ce que le fournisseur a répondu devient l'accusé de réception de la tâche.
Le canal lit les réponses, l'extracteur transcrit, le dictionnaire valide, la tâche change
d'état : completed si le fournisseur a confirmé la commande, input-required s'il y a un
reliquat, une rupture, une substitution, une réponse partielle, ou si une commande ferme
est répondue sans être confirmée. Jamais de push : l'officine relit la tâche quand elle
veut. Une réponse n'est acquittée auprès du canal qu'une fois traitée ; une panne passagère
la laisse pour la passe suivante.

Une tâche répondue (completed ou input-required) reste à l'écoute ECOUTE_JOURS jours : un
second mail du fournisseur sur la même commande (« finalement en rupture », « livraison
décalée ») est lu avec les mêmes contrôles. Une tâche à trancher (input-required) le reçoit
à la suite du premier accusé. Une tâche terminée (completed) ne redémarre jamais (A2A,
cycle de vie d'une tâche) : le second mail ouvre une tâche de suite dans le même contexte,
qui nomme la première (métadonnée suite_de, referenceTaskIds) et reprend l'écoute à sa place."""
from __future__ import annotations
import email.utils
import asyncio
import copy
import inspect
import logging
import mimetypes
import re
import uuid
from datetime import datetime, timedelta, timezone
from google.protobuf.json_format import MessageToDict
from .extraction import PasDeTranscription
from .jetons import officine_de
from a2a.auth.user import UnauthenticatedUser
from a2a.helpers import new_artifact, new_data_artifact, new_raw_part, new_text_message
from a2a.server.context import ServerCallContext
from a2a.types import ListTasksRequest, Role, Task, TaskState, TaskStatus
from .dictionnaire import MEDIA, code, marquer, reference_de, renormaliser, valider

log = logging.getLogger("pharma2a.relecture")
MAX_PIECE = 15 * 1024 * 1024    # au-delà, la pièce reste dans la boîte du pont
ORIGINES = ("erp", "transcription")
IDENTIFIANTS = ("cip13", "ean13", "ref_fournisseur")
ECOUTE_JOURS = 30
REPONDUES = (TaskState.TASK_STATE_COMPLETED, TaskState.TASK_STATE_INPUT_REQUIRED)


# Un verrou par tâche, pour tout le processus : la relecture, la répétition
# qui apprend une identité et l'annulation par l'officine relisent puis réécrivent la tâche
# entière ; sans verrou, la dernière écriture effaçait la précédente (un accusé, un état)
VERROUS_TACHES: dict[str, asyncio.Lock] = {}


def verrou_tache(tache_id: str) -> asyncio.Lock:
    return VERROUS_TACHES.setdefault(tache_id, asyncio.Lock())


def contexte_robot() -> ServerCallContext:
    """Le même propriétaire que les requêtes entrantes (pas d'authentification Starlette :
    l'utilisateur est « anonyme » des deux côtés), pour retrouver les tâches dans le magasin."""
    return ServerCallContext(user=UnauthenticatedUser())


def a_l_ecoute(t, maintenant: datetime | None = None) -> bool:
    """Une tâche répondue il y a moins de ECOUTE_JOURS jours, qui avait envoyé quelque chose."""
    if t.status.state not in REPONDUES or not t.HasField("metadata"):
        return False
    if not MessageToDict(t.metadata).get("message_id") or not t.status.HasField("timestamp"):
        return False
    quand = t.status.timestamp.ToDatetime(tzinfo=timezone.utc)
    return (maintenant or datetime.now(timezone.utc)) - quand <= timedelta(days=ECOUTE_JOURS)


async def taches_en_cours(store) -> list:
    """Les tâches qui attendent une réponse (« working »), et les tâches répondues encore à
    l'écoute d'un second mail, avec leur métadonnée."""
    ctx = contexte_robot()
    # le magasin pagine par date de dernier changement : une tâche mise à jour pendant la lecture
    # peut sauter une page ; on relit jusqu'à deux lectures identiques (revue du 4 novembre)
    vues: dict = {}
    for n in range(5):
        avant = set(vues)
        jeton = ""
        while True:
            rep = await store.list(ListTasksRequest(page_size=100, page_token=jeton, include_artifacts=True), ctx)
            for t in rep.tasks:
                vues[t.id] = t
            jeton = rep.next_page_token
            if not jeton:
                break
        if n and set(vues) == avant:
            break
    taches = list(vues.values())
    # une tâche qui a une suite n'écoute plus, quel que soit l'état de la suite : une suite
    # fermée par l'officine ferme le fil entier
    suivies = {suite_de(t) for t in taches} - {""}
    return [t for t in taches if (t.status.state == TaskState.TASK_STATE_WORKING or a_l_ecoute(t)) and t.id not in suivies]


def suite_de(t) -> str:
    """L'identifiant de la tâche dont celle-ci est la suite, vide sinon."""
    return str(MessageToDict(t.metadata).get("suite_de") or "") if t.HasField("metadata") else ""


async def envois_clos(store, vivants: set, index=None) -> tuple[set, dict]:
    """(les identifiants des mails envoyés par des tâches qui ne sont plus en cours, et
    l'historique : pour chaque référence, les couples (agent, officine) qui l'ont portée, tâches
    fermées comprises). Une réponse qui cite un envoi fermé répond à cet envoi-là ; une réponse
    sans fil sur une référence que deux officines ont portée ne se rattache pas par la référence."""
    ctx = contexte_robot()
    out, histo, jeton = set(), {}, ""
    while True:
        rep = await store.list(ListTasksRequest(page_size=100, page_token=jeton, include_artifacts=False), ctx)
        for t in rep.tasks:
            if not t.HasField("metadata"):
                continue
            meta = MessageToDict(t.metadata)
            if reference_de(meta).strip() and meta.get("message_id"):
                ref_t = reference_de(meta).strip().upper()
                dest = ",".join(sorted(a.lower() for a in _adresses(meta.get("destinataires"))))
                histo.setdefault(ref_t, set()).add((str(meta.get("agent") or ""), officine_de(meta), dest))
                if index is not None:
                    # le registre durable apprend chaque envoi croisé : une tâche qu'une page
                    # manquera demain y est déjà
                    index.inscrire((str(meta.get("agent") or ""), officine_de(meta), ref_t, str(meta.get("nature") or "commande")),
                                   t.id, t.status.state == TaskState.TASK_STATE_WORKING, dest)
            if t.status.state == TaskState.TASK_STATE_WORKING:
                continue
            mid = meta.get("message_id")
            if mid and mid not in vivants:
                out.add(str(mid))
        jeton = rep.next_page_token
        if not jeton:
            break
    if index is not None:
        for ref_t in list(histo):
            histo[ref_t] |= index.porteurs(ref_t)
    return out, histo


def id_ligne(l) -> str:
    """La clé d'une ligne : son id_ligne, à défaut son code (les commandes d'avant l'id_ligne)."""
    return str(l.get("id_ligne") or code(l))


def completer_id_ligne(accuse: dict, commande: dict | None) -> None:
    """Un accusé transcrit d'une réponse humaine nomme la ligne par son code ; l'id_ligne est
    celui de la commande, retrouvé par ce code, chaque identifiant cherché dans sa propre
    colonne (un CIP ne se compare qu'à un CIP). Ce n'est pas une invention : c'est une
    jointure sur la clé que l'officine a choisie. Sans correspondance unique, rien n'est ajouté."""
    if not commande or not isinstance(accuse.get("lignes"), list):
        return
    index: dict[tuple[str, str], set[str]] = {}
    for l in commande.get("lignes") or []:
        if isinstance(l, dict):
            for k in IDENTIFIANTS:
                if l.get(k):
                    index.setdefault((k, str(l[k])), set()).add(id_ligne(l))
    for l in accuse["lignes"]:
        if isinstance(l, dict) and not l.get("id_ligne"):
            trouves = set()
            for k in IDENTIFIANTS:
                if l.get(k):
                    trouves |= index.get((k, str(l[k])), set())
            if len(trouves) == 1:
                l["id_ligne"] = trouves.pop()


def confronter(accuse: dict, meta: dict, commande: dict | None) -> str | None:
    """Ce que l'accusé doit avoir en commun avec la commande : sa référence, des id_ligne qui
    existent dans la commande et ne se répètent pas, et sur chaque ligne des codes qui sont
    ceux de la ligne désignée. Rend le motif du refus, ou None."""
    ref = reference_de(meta).strip().upper()
    if str(accuse.get("reference") or "").strip().upper() != ref:
        return f"référence {accuse.get('reference')!r} au lieu de {ref}"
    lignes = [l for l in accuse.get("lignes") or [] if isinstance(l, dict)]
    ids = [str(l.get("id_ligne")) for l in lignes]
    doublons = sorted({i for i in ids if ids.count(i) > 1})
    if doublons:
        return "id_ligne en double : " + ", ".join(doublons)
    if commande:
        commandees = {id_ligne(l): l for l in commande.get("lignes") or [] if isinstance(l, dict)}
        inconnus = sorted(set(ids) - set(commandees))
        if inconnus:
            return "id_ligne inconnu(s) de la commande : " + ", ".join(inconnus)
        for l in lignes:
            c = commandees[str(l.get("id_ligne"))]
            for k in IDENTIFIANTS:
                if not l.get(k):
                    continue
                if not c.get(k):
                    return f"ligne {l.get('id_ligne')} : {k} {l[k]} que la ligne commandée ne portait pas"
                if str(l[k]) != str(c[k]):
                    return f"ligne {l.get('id_ligne')} : {k} {l[k]} n'est pas celui de la ligne commandée ({c[k]})"
    return None


def dernier_accuse(t) -> dict | None:
    """Le dernier accusé de réception attaché à la tâche : une tâche à l'écoute peut en avoir plusieurs."""
    for art in reversed(list(t.artifacts)):
        if art.name != "accuse_reception":
            continue
        for p in MessageToDict(art).get("parts") or []:
            if isinstance(p.get("data"), dict):
                return renormaliser(p["data"])
    return None


def accuses_de(t) -> list[dict]:
    """Les accusés attachés à la tâche, dans l'ordre."""
    out = []
    for art in t.artifacts:
        if art.name == "accuse_reception":
            out += [renormaliser(p["data"]) for p in MessageToDict(art).get("parts") or [] if isinstance(p.get("data"), dict)]
    return out


def cumuler(accuses: list) -> dict:
    """Ce que l'on sait, mail après mail : la date du premier, le numéro du
    second, la rupture du troisième. Chaque valeur dite reste connue tant qu'un mail suivant
    ne la change pas ; une ligne se complète champ par champ."""
    connu, lignes = {}, {}
    for a in accuses:
        if not isinstance(a, dict):
            continue
        for k, v in a.items():
            if k == "conditions" and isinstance(v, dict) and isinstance(connu.get("conditions"), dict):
                connu["conditions"] = {**connu["conditions"], **{ck: cv for ck, cv in v.items() if cv is not None}}
            elif k != "lignes" and v is not None:
                connu[k] = v
        for l in a.get("lignes") or []:
            if isinstance(l, dict):
                b = lignes.setdefault(id_ligne(l), {})
                b.update({k: v for k, v in l.items() if v is not None})
    connu["lignes"] = list(lignes.values())
    return connu


def connu_de(t) -> dict | None:
    """Tout ce que la chaîne a appris : ce que la tâche d'origine savait (métadonnée « connu »
    d'une tâche de suite), puis les accusés de la tâche."""
    meta = MessageToDict(t.metadata) if t.HasField("metadata") else {}
    accuses = ([meta["connu"]] if isinstance(meta.get("connu"), dict) else []) + accuses_de(t)
    return cumuler(accuses) if accuses else None


def ecart(avant: dict | None, apres: dict, commande: dict | None) -> tuple[list[str], list[str]]:
    """(ce qui change, ce qui complète) entre l'accusé déjà reçu et le nouveau. Change : une
    rupture, un reliquat non accepté, une substitution, une quantité, une date ou un numéro
    différents, un refus. Complète : une date ou un numéro qui manquaient. Une ligne dont le
    nouveau mail ne parle pas ne change pas."""
    avant = avant or {}
    libelles = {id_ligne(l): (l.get("libelle") or code(l)) for l in (commande or {}).get("lignes") or [] if isinstance(l, dict)}
    commandees = {id_ligne(l): l.get("quantite") for l in (commande or {}).get("lignes") or [] if isinstance(l, dict)}
    avant_l = {id_ligne(l): l for l in avant.get("lignes") or [] if isinstance(l, dict)}
    change, complete = [], []
    for l in apres.get("lignes") or []:
        if not isinstance(l, dict):
            continue
        i = id_ligne(l)
        b = avant_l.get(i, {})
        nom = libelles.get(i, i)
        if l.get("rupture") and not b.get("rupture"):
            change.append(f"rupture sur {nom}")
        if (l.get("reliquat") or 0) > 0 and l.get("reliquat") != b.get("reliquat") and reliquat_accepte(commande, i) is not True:
            change.append(f"reliquat de {l['reliquat']} sur {nom}")
        if l.get("cip13_substitue") and l.get("cip13_substitue") != b.get("cip13_substitue"):
            change.append(f"substitution proposée pour {nom}")
        q = l.get("quantite_confirmee")
        qb = b.get("quantite_confirmee") if b.get("quantite_confirmee") is not None else commandees.get(i)
        if q is not None and qb is not None and int(q) != int(qb):
            change.append(f"{nom} : {int(q)} au lieu de {int(qb)}")
        # Le prix net dit par le fournisseur : changé, le pharmacien tranche ; donné pour
        # la première fois, il complète
        pn, pnb = l.get("prix_net_ht"), b.get("prix_net_ht")
        if pn is not None and pnb is not None and float(pn) != float(pnb):
            change.append(f"{nom} : prix net {_euros(pn)} HT au lieu de {_euros(pnb)}")
        elif pn is not None and pnb is None:
            complete.append(f"{nom} : prix net {_euros(pn)} HT")
        # le tarif, la remise et la TVA de la ligne, tels que le fournisseur les dit : changés, le
        # pharmacien tranche ; donnés pour la première fois, ils complètent (rien ne se perd)
        for cle, dire in (("prix_tarif_ht", lambda v: f"tarif {_euros(v)} HT"), ("remise_pct", lambda v: f"remise {float(v):g} %"),
                          ("taux_tva", lambda v: f"TVA {float(v):g} %")):
            v, vb = l.get(cle), b.get(cle)
            if v is not None and vb is not None and float(v) != float(vb):
                change.append(f"{nom} : {dire(v)} au lieu de {dire(vb).split(' ', 1)[1]}")
            elif v is not None and vb is None:
                complete.append(f"{nom} : {dire(v)}")
    # les conditions dites par le fournisseur (franco, délai, catalogue joint) : changées, le
    # pharmacien tranche ; données pour la première fois, elles complètent (revue du 5 novembre)
    ca, cb = apres.get("conditions") or {}, avant.get("conditions") or {}
    dire_c = {"franco_ht": lambda v: f"franco {float(v):g} € HT", "delai_livraison_jours": lambda v: f"livraison sous {int(v)} j",
              "catalogue_joint": lambda v: "catalogue joint"}
    for k, v in (ca.items() if isinstance(ca, dict) else []):
        if not renseigne(v):
            continue
        vb = cb.get(k) if isinstance(cb, dict) else None
        mot_c = dire_c.get(k, lambda x, k=k: f"{k} {x}")
        if vb is not None and vb != v:
            change.append(f"{mot_c(v)} au lieu de {mot_c(vb)}")
        elif vb is None:
            complete.append(mot_c(v))
    d, db = apres.get("date_livraison_prevue"), avant.get("date_livraison_prevue")
    if d and db and d != db:
        change.append(f"livraison décalée au {d}")
    elif d and not db:
        complete.append(f"livraison prévue le {d}")
    r, rb = apres.get("remise_pct"), avant.get("remise_pct")
    if r is not None and rb is not None and float(r) != float(rb):
        change.append(f"remise de la commande {float(r):g} % au lieu de {float(rb):g} %")
    elif r is not None and rb is None:
        complete.append(f"remise de la commande {float(r):g} %")
    n, nb = apres.get("numero_commande_labo"), avant.get("numero_commande_labo")
    if n and nb and n != nb:
        change.append(f"numéro de commande changé ({n})")
    elif n and not nb:
        complete.append(f"commande {n} chez le fournisseur")
    if apres.get("confirmation_globale") is False and avant.get("confirmation_globale") is not False:
        change.append("le fournisseur dit ne pas prendre la commande")
    elif apres.get("confirmation_globale") is True and avant.get("confirmation_globale") is not True:
        # une confirmation explicite qui n'était pas encore dite : elle s'enregistre ; l'état de la
        # tâche ne bouge pas pour autant, le pharmacien relit ce qui restait à trancher
        complete.append("le fournisseur confirme la commande")
    return change, complete


def renseigne(v) -> bool:
    """Une condition est dite si elle porte une valeur : zéro en est une (un franco à 0, un délai
    de 0 jour), le faux et le vide n'en sont pas. (« 0 in (None, False) » est vrai en Python.)"""
    return v is not None and v is not False and v != ""


def _euros(x) -> str:
    return f"{float(x):.2f}".replace(".", ",") + " €"


def reliquat_accepte(commande: dict | None, ident: str):
    """Ce que l'officine a dit du reliquat pour cette ligne : la ligne prime sur la commande.
    True, False, ou None quand elle n'a rien dit."""
    for l in (commande or {}).get("lignes") or []:
        if isinstance(l, dict) and id_ligne(l) == ident and l.get("reliquat_accepte") is not None:
            return bool(l["reliquat_accepte"])
    v = (commande or {}).get("reliquat_accepte")
    return None if v is None else bool(v)


def a_trancher(accuse: dict, commande: dict | None = None) -> bool:
    """Reliquat, rupture, substitution, ou une quantité confirmée différente de la quantité
    commandée (un colisage imposé par le fournisseur) : le pharmacien tranche. Un reliquat
    sur une ligne où l'officine l'a accepté d'avance ne demande rien : il suit."""
    commandees = {id_ligne(l): int(l["quantite"]) for l in (commande or {}).get("lignes") or [] if isinstance(l, dict)}
    for l in accuse.get("lignes") or []:
        reliquat = (l.get("reliquat") or 0) > 0 and reliquat_accepte(commande, id_ligne(l)) is not True
        if l.get("rupture") or reliquat or l.get("cip13_substitue"):
            return True
        q = l.get("quantite_confirmee")
        if q is not None and id_ligne(l) in commandees and int(q) + int(l.get("reliquat") or 0) != commandees[id_ligne(l)]:
            return True
    return False


def _domaine(adresse) -> str:
    return str(adresse or "").rsplit("@", 1)[-1].strip().lower()


def adresses_connues(config) -> list[str]:
    """Les tâches les plus anciennes n'ont pas leurs destinataires : ceux de l'annuaire."""
    out = [a for a in (config.destinataire, config.copie) if a]
    if getattr(config, "annuaire", None):
        from .annuaire import charger_fichier
        for e in charger_fichier(config.annuaire).values():
            out += [a for a in (e.get("destinataire"), e.get("copie")) if a]
    return out


def _adresses(liste) -> list[str]:
    return [x.strip() for a in (liste or []) if a for x in re.split(r"[,;]", str(a)) if x.strip()]


def copies_connues(config) -> list[str]:
    """Les adresses d'officine que l'agent met en copie : la sienne et celles de l'annuaire."""
    out = _adresses([config.copie])
    if getattr(config, "annuaire", None):
        from .annuaire import charger_fichier
        out += _adresses([e.get("copie") for e in charger_fichier(config.annuaire).values()])
    return out


def vient_du_fournisseur(de, destinataires, config, copies=None) -> bool:
    """Le mail vient du domaine d'une adresse de fournisseur à qui la commande est partie. Sans
    expéditeur, sans domaine connu, ou depuis l'officine (les copies de cet envoi et toutes
    celles que l'agent connaît) ou l'agent lui-même : non."""
    if not de:
        return False
    d = _domaine(de)
    adresse = email.utils.parseaddr(str(de))[1].strip().lower()
    exp = email.utils.parseaddr(str(config.expediteur or ""))[1].strip().lower()
    if exp and adresse == exp:
        return False
    # L'ADRESSE EXACTE DU FOURNISSEUR PASSE (revue du 4 novembre) : un fournisseur dont la
    # boîte partage le domaine de l'agent ou de l'officine (une messagerie grand public) était
    # écarté avec tout son domaine, même en citant le bon fil. Seul le domaine se juge prudemment.
    if adresse and adresse in {email.utils.parseaddr(a)[1].strip().lower() for a in _adresses(destinataires)}:
        return True
    if exp and d == _domaine(exp):
        return False
    fournisseur = {_domaine(a) for a in _adresses(destinataires)}
    # une copie sur le domaine d'un destinataire est un second contact du fournisseur (un
    # délégué) ; une copie sur un autre domaine est l'officine
    officine = {_domaine(a) for a in _adresses(copies) + copies_connues(config)} - fournisseur
    if d in officine:
        return False
    return d in fournisseur


# ce qui ne peut pas suivre une référence sans la prolonger : CMD-42 n'est pas dans CMD-42-2,
# mais « commande CMD-42. » et « CMD-42 - Pharmacie » la contiennent
FRONTIERE = r"(?![A-Z0-9_/-]|\.[A-Z0-9])"


def references_dans(r, connues) -> list[str]:
    """Les références attendues que la réponse nomme : celle de l'en-tête posé par l'agent si
    elle est connue, sinon celles qui apparaissent entières dans l'objet ou le texte. Une
    référence déduite par le canal (son raccourci s'arrête à l'espace, « BC-42 » dans
    « BC-42 2 ») ne vaut pas plus que le texte où elle a été lue."""
    if getattr(r, "reference_explicite", False) and r.reference and r.reference.strip().upper() in connues:
        return [r.reference.strip().upper()]
    texte = ((r.objet or "") + "\n" + (r.texte or "")).upper()
    motif = lambda k: re.compile(r"(?<![A-Z0-9._/-])" + re.escape(k) + FRONTIERE)
    spans = {k: [m.span() for m in motif(k).finditer(texte)] for k in connues}
    trouvees = [k for k, s in spans.items() if s]
    # « BC-42 » apparaît dans « BC-42 2 » : une occurrence recouverte par une référence plus longue
    # ne compte pas ; citée à part, elle compte, et les deux références sont alors nommées
    def a_part(k):
        longues = [s for autre in trouvees if autre != k and k in autre for s in spans[autre]]
        return any(not any(a <= d and f <= b for a, b in longues) for d, f in spans[k])
    return [k for k in trouvees if a_part(k)]


CLOS = object()      # la réponse cite un envoi dont la tâche n'est plus en cours
AMBIGU = object()    # une autre officine a porté cette référence : on ne rattache pas par la référence


def _uniques(paires: list) -> list:
    vus, out = set(), []
    for t, meta in paires:
        if t.id not in vus:
            vus.add(t.id); out.append((t, meta))
    return out


def candidates_de(r, par_mid: dict, mids_clos: set, par_ref: dict, config, histo: dict | None = None) -> list:
    """Les tâches en cours auxquelles cette réponse peut répondre, dans l'ordre de certitude :
    celle dont la réponse cite l'identifiant du mail envoyé (In-Reply-To, puis le fil
    References), sinon celles dont la référence est nommée, et dont le fournisseur est bien
    l'expéditeur. Une réponse qui cite un envoi dont la tâche est fermée (annulée, remplacée,
    terminée) répond à cet envoi-là, pas au suivant sous la même référence : [CLOS]."""
    # In-Reply-To d'abord ; puis le fil References, du plus récent au plus ancien : le premier
    # envoi connu décide, qu'il soit ouvert ou clos
    cites_tous = list(getattr(r, "en_reponse_a", None) or []) + list(getattr(r, "fil", None) or [])
    for cites in (getattr(r, "en_reponse_a", None) or [], getattr(r, "fil", None) or []):
        for m in reversed(cites):
            if m in par_mid:
                return [par_mid[m]]
            if m in mids_clos:
                return [CLOS]
    # UN FIL INCONNU NE SE DEVINE PAS PAR LA RÉFÉRENCE (revue du 5 novembre) : un mail qui cite un
    # envoi qu'on ne connaît pas répond à autre chose, ou à un envoi plus ancien que la mémoire ;
    # le rattacher par la référence pourrait le donner à une autre officine. Il attend.
    if cites_tous and getattr(r, "accuse", None) is None:
        return []
    out = []
    refs = references_dans(r, par_ref)
    for ref in refs:
        out += par_ref.get(ref, [])
    # ... et une référence que deux officines ont portée, même si une seule est encore en cours
    # (l'autre fermée, ou sortie de l'écoute), reste ambiguë : un mail retardé de la première irait
    # à la seconde. L'historique des tâches le dit.
    if histo and out:
        porteurs = set().union(*(histo.get(ref, set()) for ref in refs))
        for _, m in out:
            # les porteurs compatibles avec cette tâche : le même agent, ou un autre agent qui écrit
            # à la même adresse de fournisseur (deux agents pour deux officines d'un même labo) ;
            # deux couples (agent, officine) différents parmi eux, et la référence est ambiguë
            agent_c = str(m.get("agent") or "")
            dest_c = {a.lower() for a in _adresses(m.get("destinataires") or adresses_connues(config))}
            couples = {(a, o) for a, o, d in porteurs if a == agent_c or (dest_c & set(d.split(",")) - {""})}
            if len(couples) > 1:
                return out if len(out) > 1 else out + [AMBIGU]
    # L'expéditeur d'abord : les priorités qui suivent ne départagent que les
    # tâches de ce fournisseur ; la commande en cours d'un autre ne fait plus écarter la
    # sienne. Un accusé structuré vient du système du fournisseur : pas d'expéditeur à vérifier.
    out = [(t, meta) for t, meta in _uniques(out)
           if getattr(r, "accuse", None) is not None
           or vient_du_fournisseur(r.de, meta.get("destinataires") or adresses_connues(config), config, meta.get("copies"))]
    # SANS LE FIL, UNE SEULE COMMANDE POSSIBLE OU RIEN (idée reprise du pont de référence) : une
    # réponse qui ne cite pas l'envoi n'est rattachée d'office que si une seule commande en cours,
    # chez ce fournisseur, porte cette référence. Sinon on ne choisit pas pour elle (deux officines,
    # ou une proposition et sa commande ferme, ou une commande et sa relance) : elle reste dans la
    # boîte, et le fournisseur est invité à répondre dans le fil, ou le pharmacien tranche.
    return out


def _meme_officine(paires: list) -> bool:
    """Toutes ces tâches sont-elles d'une même officine ? Chacune partage au moins une identité
    (compte, CIP, adresse) avec chacune des autres ; une tâche sans identité n'est à personne."""
    ids = [set(m.get("identites") or []) for _, m in paires]
    return all(ids[i] and ids[j] and ids[i] & ids[j] for i in range(len(ids)) for j in range(i + 1, len(ids)))


def _date_comparable(d) -> datetime:
    """Une date de mail comparable à toute autre : « -0000 » donne une date sans fuseau, « +0000 »
    une date avec ; sans fuseau, on la prend en UTC (RFC 5322). Sans date, la plus ancienne."""
    if not isinstance(d, datetime):
        return datetime.min.replace(tzinfo=timezone.utc)
    return d if d.tzinfo is not None else d.replace(tzinfo=timezone.utc)


def nature_de(meta: dict, commande: dict | None) -> str:
    return str(meta.get("nature") or (commande or {}).get("nature") or "commande")


async def relire(config, store, backend, extracteur, depuis: datetime | None = None, index=None) -> int:
    """Une passe. Rend le nombre de tâches mises à jour. Chaque réponse traitée, retenue ou
    écartée pour de bon, est acquittée auprès du canal ; une réponse qu'une panne passagère
    empêche de lire reste dans la boîte pour la passe suivante."""
    try:
        # Le réseau hors de la boucle : la boîte et le modèle répondent en
        # secondes ; dans la boucle, ils figeraient l'agent pendant ce temps
        reponses = await asyncio.to_thread(backend.lire_reponses, depuis)
    except Exception as e:
        log.warning("lecture des réponses impossible : %s", e)
        return 0
    if not reponses:
        return 0
    origine = getattr(backend, "origine", None)
    if origine not in ORIGINES:
        log.error("le canal ne déclare pas l'origine de ses accusés (origine = %r) : aucune réponse lue", origine)
        return 0
    taches = await taches_en_cours(store)
    par_ref: dict[str, list] = {}
    par_mid: dict[str, tuple] = {}
    for t in taches:
        meta = MessageToDict(t.metadata) if t.HasField("metadata") else {}
        if reference_de(meta).strip():
            # la référence se compare normalisée : un espace en bout ne sépare pas deux commandes
            par_ref.setdefault(reference_de(meta).strip().upper(), []).append((t, meta))
            if meta.get("message_id"):
                par_mid[str(meta["message_id"])] = (t, meta)
    mids_clos, histo = await envois_clos(store, set(par_mid), index)
    if index is not None:
        # les références en cours dont le parcours n'aurait rien vu : le registre les connaît
        for ref_t in par_ref:
            histo.setdefault(ref_t, set()).update(index.porteurs(ref_t))
    n, lus = 0, []
    # L'ORDRE DU FIL SE GARDE (revue du 4 novembre) : les réponses se lisent de la plus ancienne à
    # la plus récente, et une réponse qu'une panne passagère retient retient aussi les suivantes
    # de la même tâche, pour qu'une date déjà corrigée ne revienne jamais après la correction
    retenues: set = set()
    for _, r in sorted(enumerate(reponses), key=lambda ix: (_date_comparable(ix[1].recu_le), ix[0])):
        cand = candidates_de(r, par_mid, mids_clos, par_ref, config, histo)
        tid = cand[0][0].id if len(cand) == 1 and cand[0] not in (CLOS, AMBIGU) else None
        if tid and tid in retenues:
            log.info("réponse %s : une réponse plus ancienne de cette tâche attend, elle attend aussi", r.identifiant)
            continue
        try:
            suite, lu = await _relire_une(config, store, extracteur, origine, par_mid, mids_clos, par_ref, r, histo)
        except Exception:
            log.exception("réponse %s : erreur inattendue, elle reste dans la boîte", r.identifiant)
            if tid:
                retenues.add(tid)
            continue
        if not lu and tid:
            retenues.add(tid)
        n += suite
        if lu:
            lus.append(r.identifiant)
    if lus and hasattr(backend, "acquitter"):
        try:
            await asyncio.to_thread(backend.acquitter, lus)
        except Exception as e:
            log.warning("acquittement impossible (%s) : les réponses seront relues", e)
    return n


def _retirer(par_mid: dict, par_ref: dict, mids_clos: set, tache_id: str) -> None:
    """La tâche a changé d'état dans cette passe : elle n'attend plus de réponse, et son envoi
    est clos, une seconde réponse au même mail ne doit pas retomber sur la référence."""
    for ref in list(par_ref):
        par_ref[ref] = [(t, m) for t, m in par_ref[ref] if t.id != tache_id]
        if not par_ref[ref]:
            del par_ref[ref]
    for mid in [m for m, (t, _) in par_mid.items() if t.id == tache_id]:
        del par_mid[mid]
        mids_clos.add(mid)


def _remplacer(par_mid: dict, par_ref: dict, tache_id: str, suivante) -> None:
    """La tâche a désormais une suite : dans cette passe aussi, c'est la suite qui écoute."""
    meta = MessageToDict(suivante.metadata)
    for ref in par_ref:
        par_ref[ref] = [((suivante, meta) if t.id == tache_id else (t, m)) for t, m in par_ref[ref]]
    for mid in [m for m, (t, _) in par_mid.items() if t.id == tache_id]:
        par_mid[mid] = (suivante, meta)


async def _relire_une(config, store, extracteur, origine, par_mid, mids_clos, par_ref, r, histo=None) -> tuple[int, bool]:
    """Une réponse. Rend (1 si une tâche a changé, True si la réponse est à acquitter)."""
    candidates = candidates_de(r, par_mid, mids_clos, par_ref, config, histo)
    if candidates == [CLOS]:
        log.info("réponse %s de %s : elle répond à un envoi dont la tâche est fermée, ignorée", r.identifiant, r.de)
        return 0, True
    if not candidates:
        # une réponse qui cite un envoi (In-Reply-To, References) qu'on ne retrouve pas n'est pas
        # écartée : son rattachement est incertain, elle attend la passe suivante plutôt que se perdre
        if (getattr(r, "en_reponse_a", None) or getattr(r, "fil", None)) and getattr(r, "accuse", None) is None:
            log.info("réponse %s de %s : elle cite un envoi introuvable pour l'instant, elle attend", r.identifiant, r.de)
            return 0, False
        log.info("réponse %s de %s : aucune commande en cours à laquelle elle réponde (référence %s), ignorée",
                 r.identifiant, r.de, r.reference)
        return 0, True
    if len(candidates) > 1:
        log.warning("réponse %s : %s tâches en cours peuvent la recevoir, rattachement ambigu, on attend", r.identifiant, len(candidates))
        return 0, False
    t, meta = candidates[0]
    structure = getattr(r, "accuse", None) is not None
    # Seul le fournisseur répond : un transfert de l'officine à son commercial porte
    # la même référence ; lu comme une réponse, il avait « confirmé » une commande. Le mail
    # doit venir du domaine d'une adresse à qui la commande est partie, et ce que la boîte
    # sait de son authenticité (DKIM, SPF, DMARC) ne doit pas dire le contraire.
    if not structure and not vient_du_fournisseur(r.de, meta.get("destinataires") or adresses_connues(config), config, meta.get("copies")):
        log.info("réponse %s de %s : pas le fournisseur, ignorée", r.identifiant, r.de)
        return 0, True
    verdict = getattr(r, "authentifie", None)
    if not structure and (verdict is False or (verdict is None and getattr(config, "exiger_authentification", False))):
        log.warning("réponse %s de %s : expéditeur %s par la boîte, ignorée", r.identifiant, r.de,
                    "non authentifié" if verdict is False else "sans verdict d'authentification")
        return 0, True
    commande = renormaliser(meta.get("commande")) if meta.get("commande") else None
    if structure:
        # L'accusé du système du fournisseur : pris tel quel, validé et confronté plus bas
        accuse = renormaliser(copy.deepcopy(r.accuse))
    else:
        try:
            kw = {"identifiant": r.identifiant} if "identifiant" in inspect.signature(extracteur.extraire).parameters else {}
            # le modèle apprend qu'il y a des pièces jointes (un catalogue joint se dit), jamais leur
            # nom, qui peut porter celui de l'officine ou son compte, ni leur contenu
            texte = r.texte + ((f"\n\n[{len(r.pieces)} pièce(s) jointe(s) au message]") if r.pieces else "")
            accuse = renormaliser(await asyncio.to_thread(extracteur.extraire, texte, commande, **kw))
        except PasDeTranscription as e:
            log.warning("réponse %s : pas de transcription possible (%s), elle attend dans la boîte", r.identifiant, e)
            return 0, False
        except RuntimeError as e:
            # l'extracteur a répondu non (refus du modèle) : définitif
            log.warning("réponse %s : extraction refusée (%s), la tâche reste en cours", r.identifiant, e)
            return 0, True
        except Exception as e:
            # le modèle ou le réseau sont indisponibles : la réponse attend la passe suivante
            log.warning("réponse %s : extraction impossible (%s), la réponse sera relue", r.identifiant, e)
            return 0, False
    if not isinstance(accuse, dict):
        log.warning("réponse %s : l'extracteur n'a pas rendu un objet, la tâche reste en cours", r.identifiant)
        return 0, True
    accuse.setdefault("version", "1")
    if not accuse.get("reference"):
        accuse["reference"] = reference_de(meta)
    # L'origine est celle du canal, jamais celle que l'extracteur écrirait : un mail lu
    # par un modèle est une transcription, quoi qu'en dise le JSON produit
    accuse["origine"] = origine
    completer_id_ligne(accuse, commande)
    erreurs = valider("accuse_reception", accuse)
    if erreurs:
        log.warning("réponse %s : accusé non conforme au dictionnaire (%s), la tâche reste en cours", r.identifiant, " ; ".join(erreurs[:5]))
        return 0, True
    motif = confronter(accuse, meta, commande)
    if motif:
        log.warning("réponse %s : l'accusé ne répond pas à cette commande (%s), la tâche reste en cours", r.identifiant, motif)
        return 0, True
    # Un mail qui ne dit rien n'est pas une confirmation : ni ligne, ni numéro, ni date,
    # ni condition, la commande n'est pas « confirmée telle que commandée », elle reste en cours ;
    # des conditions toutes nulles ne disent rien non plus, ni une ligne qui ne porte qu'une
    # question (« pourquoi de la gelée royale ? » avait confirmé la commande telle quelle)
    lignes = [l for l in accuse.get("lignes") or [] if isinstance(l, dict)]
    conditions = accuse.get("conditions") or {}
    dit_une_condition = any(renseigne(v) for v in conditions.values()) if isinstance(conditions, dict) else bool(conditions)
    def parle_quantite(l):
        # un reliquat à zéro seul ne dit rien : le modèle peut l'écrire par défaut
        return (l.get("rupture") is True or l.get("quantite_confirmee") not in (None, "")
                or (l.get("reliquat") or 0) > 0 or bool(l.get("cip13_substitue")))
    repondues = {id_ligne(l) for l in lignes if parle_quantite(l)}
    dit_un_colisage = any(l.get("colisage") not in (None, "") for l in lignes)
    # un prix, une remise ou une TVA dits par le fournisseur se gardent, même seuls
    dit_un_prix = accuse.get("remise_pct") is not None or any(
        l.get(k) is not None for l in lignes for k in ("prix_tarif_ht", "remise_pct", "prix_net_ht", "taux_tva"))
    # Le fournisseur peut confirmer globalement : un numéro ou une date (la commande est
    # enregistrée), ou confirmation_globale à true (il l'a dit en toutes lettres) ; à false,
    # il dit qu'il ne la prend pas, et le pharmacien tranche
    global_dit = bool(accuse.get("numero_commande_labo") or accuse.get("date_livraison_prevue")
                      or accuse.get("confirmation_globale") is True)
    refus = accuse.get("confirmation_globale") is False
    confirme = bool(repondues or global_dit)
    if not (confirme or refus or dit_un_colisage or dit_une_condition or dit_un_prix):
        log.info("réponse %s : rien sur la commande, la tâche reste en cours", r.identifiant)
        return 0, True
    async with verrou_tache(t.id):
        # La tâche est relue juste avant d'écrire : une autre réponse de la même passe, ou une
        # fermeture par l'officine entre-temps, l'ont peut-être déjà sortie de « en cours »
        courant = await store.get(t.id, contexte_robot())
        if courant is None or (courant.status.state != TaskState.TASK_STATE_WORKING and not a_l_ecoute(courant)):
            log.info("réponse %s : la tâche %s n'est plus en cours, ignorée", r.identifiant, t.id)
            _retirer(par_mid, par_ref, mids_clos, t.id)
            return 0, True
        t = courant
        # Une réponse ne se traite qu'une fois : si l'acquittement a échoué après
        # l'écriture, elle revient à la passe suivante ; la chaîne la reconnaît et l'acquitte
        if r.identifiant in (MessageToDict(t.metadata).get("reponses_lues") or [] if t.HasField("metadata") else []):
            log.info("réponse %s : déjà portée par la tâche %s, acquittée sans rien refaire", r.identifiant, t.id)
            return 0, True
        pieces = [(n, b) for n, b in r.pieces if b and len(b) <= MAX_PIECE]
        if t.status.state in REPONDUES:
            n, lu, suivante = await _second_mail(store, t, r, accuse, commande, pieces, structure)
            if suivante is not None:
                _remplacer(par_mid, par_ref, t.id, suivante)
            return n, lu
        trancher = a_trancher(accuse, commande)
        nature = nature_de(meta, commande)
        sans_confirmation = nature == "commande" and not confirme and not refus
        # La date ferme (cinq pharmaciens interrogés sur six la veulent) : si la
        # commande l'exige, une confirmation sans date de livraison n'en est pas une
        sans_date = (nature == "commande" and confirme and not refus and not accuse.get("date_livraison_prevue")
                     and (commande or {}).get("date_livraison_exigee") is True)
        # Une ligne confirmée ne confirme pas les autres : sans numéro ni date, les lignes dont le
        # fournisseur ne parle pas restent inconnues, et le pharmacien tranche
        toutes = {id_ligne(l) for l in (commande or {}).get("lignes") or [] if isinstance(l, dict)}
        sans_reponse = sorted(toutes - repondues) if (nature == "commande" and confirme and not global_dit) else []
        t.artifacts.append(marquer(new_data_artifact("accuse_reception", accuse, media_type=MEDIA["accuse_reception"],
                                             description=("Accusé de réception rendu par le système du fournisseur" if structure
                                                          else "Accusé de réception du fournisseur, transcrit depuis sa réponse"))))
        # Les pièces jointes remontent : un catalogue, un tarif, un bon signé. Le pont ne
        # les lit pas ; il les attache à la tâche, telles quelles, pour l'agent de l'officine.
        _joindre(t, pieces)
        resume = [f"Réponse du fournisseur lue le {r.recu_le.strftime('%d/%m/%Y')}"]
        if accuse.get("numero_commande_labo"):
            resume.append(f"commande {accuse['numero_commande_labo']} chez le fournisseur")
        if accuse.get("date_livraison_prevue"):
            resume.append(f"livraison prévue le {accuse['date_livraison_prevue']}")
        if pieces:
            resume.append(f"{len(pieces)} pièce(s) jointe(s)")
        suivent = [l for l in lignes if (l.get("reliquat") or 0) > 0 and reliquat_accepte(commande, id_ligne(l)) is True]
        if suivent:
            resume.append(f"{len(suivent)} ligne(s) en reliquat, accepté d'avance par l'officine")
        if refus:
            resume.append("le fournisseur dit ne pas prendre la commande : à trancher par le pharmacien")
        elif sans_confirmation:
            # Une commande ferme n'est confirmée que si le fournisseur le dit : un colisage, une
            # condition ou un catalogue seuls ne confirment pas les quantités ; le pharmacien tranche
            resume.append("colisage, prix, conditions ou catalogue sans confirmation de la commande : à trancher par le pharmacien")
        elif trancher:
            resume.append("quantité changée, reliquat, rupture ou substitution : à trancher par le pharmacien")
        elif sans_date:
            resume.append("sans date de livraison ferme, que la commande exigeait : à trancher par le pharmacien")
        elif sans_reponse:
            resume.append(f"réponse partielle, {len(sans_reponse)} ligne(s) sans réponse : à trancher par le pharmacien")
        else:
            resume.append("confirmée telle que commandée" if nature == "commande"
                          else "relance répondue" if nature == "relance" else "proposition répondue")
        a_trancher_ = trancher or sans_confirmation or bool(sans_reponse) or refus or sans_date
        t.status.state = TaskState.TASK_STATE_INPUT_REQUIRED if a_trancher_ else TaskState.TASK_STATE_COMPLETED
        t.status.message.CopyFrom(new_text_message(", ".join(resume) + ".", task_id=t.id, context_id=t.context_id))
        t.status.timestamp.FromDatetime(datetime.now(timezone.utc))
        _noter_lue(t, r)
        await store.save(t, contexte_robot())
        # la tâche reste à l'écoute : un second mail de la même passe sera lu comme une suite
        log.info("tâche %s : %s", t.id, t.status.state)
        return 1, True


def _joindre(t, pieces) -> None:
    if pieces:
        t.artifacts.append(new_artifact(
            [new_raw_part(b, media_type=mimetypes.guess_type(n)[0] or "application/octet-stream", filename=n) for n, b in pieces],
            "pieces_jointes", description="Pièces jointes à la réponse du fournisseur, telles qu'il les a envoyées"))


async def _second_mail(store, t, r, accuse, commande, pieces, structure) -> tuple[int, bool, Task | None]:
    """Le second mail du fournisseur : la tâche était répondue, il écrit encore.
    S'il change quelque chose, le pharmacien tranche ; s'il ne fait que compléter (une date,
    un numéro), l'état reste ; s'il ne dit rien de neuf, rien ne bouge. Une tâche à trancher
    reçoit le nouvel accusé à la suite du premier. Une tâche terminée ne se rouvre pas :
    une tâche de suite naît dans le même contexte et la première
    reste telle qu'elle était."""
    change, complete = ecart(connu_de(t), accuse, commande)
    if not change and not complete and not pieces:
        # Retenue comme lue même sans rien de neuf : si l'acquittement échoue, ce mail revient,
        # et après une date corrigée entre-temps il passerait pour un nouveau décalage
        log.info("réponse %s : rien de neuf pour la tâche %s, déjà répondue", r.identifiant, t.id)
        lues = list(MessageToDict(t.metadata).get("reponses_lues") or []) if t.HasField("metadata") else []
        if r.identifiant not in lues:
            _noter_lue(t, r)
            await store.save(t, contexte_robot())
        return 0, True, None
    quand = r.recu_le.strftime("%d/%m/%Y")
    if change:
        etat = TaskState.TASK_STATE_INPUT_REQUIRED
        texte = f"Nouvelle réponse du fournisseur lue le {quand} : " + ", ".join(change + complete) + " : à trancher par le pharmacien."
    else:
        etat = t.status.state
        texte = f"Complément du fournisseur lu le {quand} : " + ", ".join(complete + ([f"{len(pieces)} pièce(s) jointe(s)"] if pieces else [])) + "."
    suivante = None
    if t.status.state == TaskState.TASK_STATE_COMPLETED:
        t = suivante = suite(t, etat)
        texte = "Suite de la commande, tâche " + suite_de(t) + ". " + texte
    else:
        t.status.state = etat
    t.artifacts.append(marquer(new_data_artifact("accuse_reception", accuse, media_type=MEDIA["accuse_reception"],
                                         description=("Nouvel accusé rendu par le système du fournisseur" if structure
                                                      else "Nouvelle réponse du fournisseur, transcrite, après une première réponse"))))
    _joindre(t, pieces)
    t.status.message.CopyFrom(new_text_message(texte, task_id=t.id, context_id=t.context_id))
    t.status.timestamp.FromDatetime(datetime.now(timezone.utc))
    _noter_lue(t, r)
    await store.save(t, contexte_robot())
    log.info("tâche %s : second mail, %s", t.id, t.status.state)
    return 1, True, suivante


def _noter_lue(t, r) -> None:
    """L'identifiant de la réponse s'écrit avec ce qu'elle a changé, dans la même sauvegarde."""
    lues = list(MessageToDict(t.metadata).get("reponses_lues") or []) if t.HasField("metadata") else []
    if r.identifiant not in lues:
        # sans plafond : la liste vit avec le fil, qui n'écoute que trente jours
        t.metadata.update({"reponses_lues": lues + [r.identifiant]})


def suite(t, etat) -> Task:
    """La tâche de suite d'une tâche terminée : même contexte, même métadonnée (référence,
    commande, identités, envoi : la réponse suivante la retrouve par le même fil), et le nom
    de la première, en métadonnée et dans le message qui l'ouvre (referenceTaskIds)."""
    meta = MessageToDict(t.metadata) if t.HasField("metadata") else {}
    s = Task(id=str(uuid.uuid4()), context_id=t.context_id, status=TaskStatus(state=etat))
    s.metadata.update({**meta, "suite_de": t.id, "connu": connu_de(t) or {}})
    ouverture = new_text_message(f"Second mail du fournisseur sur une commande déjà répondue (tâche {t.id}).",
                                 task_id=s.id, context_id=s.context_id, role=Role.ROLE_AGENT)
    ouverture.reference_task_ids.append(t.id)
    s.history.append(ouverture)
    return s
