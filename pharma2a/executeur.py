"""Ce que l'agent fait d'un message : vérifier qu'il respecte le dictionnaire, le transmettre
au laboratoire par le canal, et dire où en est la tâche. Rien d'autre."""
from __future__ import annotations
import email.utils
import inspect
import logging
import re
from a2a.helpers import get_data_parts, new_data_part, new_task_from_user_message, new_text_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from . import courrier
from google.protobuf.json_format import MessageToDict
from a2a.types import ListTasksRequest, TaskState
from .dictionnaire import EXTENSION, MEDIA, cle_commande, detecter, empreinte, erreurs_de, identites, reference_de, renormaliser
from .jetons import normal, officine_du_compte, officine_de
from .fiche import DEMANDE, Fiche
from .index_commandes import Index

log = logging.getLogger("pharma2a")


def _nom(x) -> str:
    """Un nom de fournisseur comparable : sans casse, sans accents, sans ponctuation."""
    import unicodedata
    return re.sub(r"[^a-z0-9]+", "", unicodedata.normalize("NFKD", str(x)).encode("ascii", "ignore").decode().lower())


# les mots qui précèdent un nom de fournisseur sans le changer
GENERIQUES = ("laboratoires", "laboratoire", "labos", "les", "sas", "sa")


def _nom_seul(x) -> str:
    """Le nom sans casse, sans accents, sans ponctuation, et sans les mots génériques de tête."""
    import unicodedata
    mots = re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKD", str(x)).encode("ascii", "ignore").decode().lower()).split()
    while len(mots) > 1 and mots[0] in GENERIQUES:
        mots = mots[1:]
    return "".join(mots)


def _meme_fournisseur(a, b) -> bool:
    """« LABO UN », « Labo Un » et « Laboratoires Labo Un » sont le même fournisseur ; « LABO UN
    BIS », « UN » ou « AUTRE LABO » non : l'inclusion d'un nom dans l'autre ne suffit pas."""
    x, y = _nom(a), _nom(b)
    return bool(x and y) and (x == y or _nom_seul(a) == _nom_seul(b))


def _adresses_cle(s) -> str:
    """Les adresses d'un champ destinataire, normalisées, en une clé comparable."""
    return ",".join(sorted(x.strip().lower() for x in re.split(r"[,;]", s or "") if x.strip()))


def rayon_d_un_autre(nom: str, doc, fournisseur: str) -> str | None:
    """L'état de rayon, joint à une commande ou envoyé seul, est pour ce fournisseur : sinon, le
    motif du refus. Lu seulement sur un document conforme. Un agent qui n'a pas dit son nom
    (« Fournisseur » par défaut) ne compare rien. Le guichet le demande aussi avant de rendre une
    tâche existante : une répétition ne passe pas à côté de ce contrôle."""
    if not isinstance(doc, dict) or fournisseur in ("", "Fournisseur"):
        return None
    r_ = doc.get("etat_de_rayon") if nom == "commande" else (doc if nom == "etat_de_rayon" else None)
    if isinstance(r_, dict) and r_.get("fournisseur") and not _meme_fournisseur(r_["fournisseur"], fournisseur):
        return f"etat_de_rayon/fournisseur : « {r_['fournisseur']} » n'est pas le fournisseur de cet agent ({fournisseur})"
    return None


class Executeur(AgentExecutor):
    def __init__(self, config, backend, fiche: Fiche | None = None, store=None, index=None):
        self.c = config
        self.backend = backend
        self.fiche = fiche or Fiche(None)
        self.store = store
        self.index = index

    async def _commande_relancee(self, doc: dict, ctx):
        """(la tâche de la commande relancée, une relance déjà faite aujourd'hui pour ce motif).
        La commande est celle que cet agent a reçue sous la même référence, pour la même officine
        (une identité commune ; le compte du jeton, s'il est rattaché), non refusée ; la plus
        récente si la référence a servi plusieurs fois."""
        if self.store is None:
            return None, None
        auth = (getattr(getattr(ctx, "call_context", None), "state", None) or {}).get("auth")
        compte = auth.get("pharma2a.compte") if isinstance(auth, dict) else None
        ids = set(identites(doc))
        ref = reference_de(doc).strip().upper()
        commandes, deja, jeton = [], None, ""
        while True:
            rep = await self.store.list(ListTasksRequest(page_size=100, page_token=jeton, include_artifacts=False), ctx.call_context)
            for t in rep.tasks:
                meta = MessageToDict(t.metadata) if t.HasField("metadata") else {}
                if str(meta.get("agent") or "") != str(self.c.agent or "") or reference_de(meta).strip().upper() != ref:
                    continue
                if t.status.state == TaskState.TASK_STATE_REJECTED or meta.get("suite_de"):
                    continue
                # une tâche d'avant les identités n'a que sa commande : on les y relit,
                # comme le guichet le fait pour une répétition
                ids2 = set(meta.get("identites") or []) | {str(meta.get("compte") or "")} - {""}
                if isinstance(meta.get("commande"), dict):
                    ids2 |= set(identites(renormaliser(meta["commande"])))
                # la commande relancée est une tâche de la même officine (celle du jeton) ; les
                # identités du document ne départagent qu'entre les tâches de cette officine
                if officine_de(meta) != officine_du_compte(compte):
                    continue
                if not compte and not (ids2 & ids):
                    continue
                nature = str(meta.get("nature") or "commande")
                if nature == "relance":
                    if meta.get("motif") == doc["motif"] and meta.get("date") == doc["date"] and t.status.state != TaskState.TASK_STATE_FAILED:
                        deja = t
                elif nature in ("commande", "proposition") and meta.get("commande"):
                    commandes.append((nature == "commande", t.status.timestamp.ToDatetime(), t))
            jeton = rep.next_page_token
            if not jeton:
                # la commande ferme d'abord, la plus récente ; à défaut, la proposition
                commandes.sort(key=lambda x: (x[0], x[1]), reverse=True)
                return (commandes[0][2] if commandes else None), deja

    async def execute(self, ctx: RequestContext, queue: EventQueue) -> None:
        message = ctx.message
        donnees = get_data_parts(message.parts) if message else []
        doc = renormaliser(donnees[0]) if donnees else None
        if not donnees and ctx.current_task is None:
            # un texte brut n'ouvre pas de tâche : on répond par un message, et on explique
            await queue.enqueue_event(new_text_message(
                "Cet agent ne parle que le dictionnaire PharmA2A : envoyez une « commande », un « etat_de_rayon » "
                "ou une demande de fiche dans une partie de type données du message. Schémas et exemples : voir la carte étendue et la spécification (SPECIFICATION.md).",
                context_id=ctx.context_id, task_id=None))
            return
        tache = ctx.current_task
        if tache is not None:
            # Une tâche déjà ouverte ne repart pas : un message posé sur une tâche existante (avec
            # son identifiant) ne transmet rien, quel que soit son contenu. La tâche reste dans
            # son état et le dit. Une nouvelle commande est un nouveau message, sans identifiant
            # de tâche, sous une nouvelle référence.
            maj = TaskUpdater(queue, tache.id, tache.context_id)
            await maj.update_status(tache.status.state, maj.new_agent_message([new_text_part(
                "Cette tâche est déjà ouverte : rien n'est retransmis au fournisseur. Pour une nouvelle commande, "
                "envoyez un nouveau message sans identifiant de tâche, sous une nouvelle référence.")]))
            return
        # le protocole veut la tâche AVANT son premier changement d'état
        tache = new_task_from_user_message(message)
        # Toute tâche porte son agent : le magasin est partagé entre les agents de l'annuaire,
        # une tâche sans agent n'est à personne. L'identifiant de l'envoi est tiré ici, avant
        # l'envoi, pour que la réponse qui le cite (In-Reply-To) retrouve la tâche sans doute.
        domaine = self.c.expediteur.rsplit("@", 1)[-1] if "@" in (self.c.expediteur or "") else "localhost"
        message_id = email.utils.make_msgid(domain=domaine)
        # personne ne lit la tâche au titre d'un compte tant qu'un jeton ne l'a pas prouvé
        tache.metadata.update({"agent": self.c.agent or "", "message_id": message_id, "lecteurs": []})
        origine = deja = None
        relance = isinstance(doc, dict) and detecter(next((p.media_type for p in message.parts if p.HasField("data")), None),
                                                    dict(message.metadata) if message.metadata else {}, doc) == "relance"
        if relance and not erreurs_de("relance", doc):
            # La relance : elle vise la commande que cet agent a reçue sous cette référence,
            # pour cette officine ; la tâche garde la commande, pour que la réponse soit lue contre elle
            origine, deja = await self._commande_relancee(doc, ctx)
            meta_o = MessageToDict(origine.metadata) if origine is not None else {}
            adresses = lambda s: [x.strip() for x in re.split(r"[,;]", s or "") if x.strip()]
            tache.metadata.update({"reference": doc["reference"], "nature": "relance", "motif": doc["motif"], "date": doc["date"],
                                   "identites": sorted(set(identites(doc)) | set(meta_o.get("identites") or [])),
                                   "destinataires": adresses(self.c.destinataire), "copies": adresses(self.c.copie),
                                   **({"commande": meta_o["commande"], "relance_de": origine.id} if origine is not None else {})})
        # la relecture retrouve la tâche par la référence de la commande, et l'extracteur
        # relit les lignes commandées : les deux voyagent avec la tâche
        # la clé, les identités et l'empreinte se lisent sur une commande conforme seulement : un
        # document mal formé (« lignes » qui n'est pas une liste) est refusé plus bas, en clair
        if isinstance(doc, dict) and doc.get("reference") and not erreurs_de("commande", doc):
            compte, ref, nature = cle_commande(doc)
            # destinataires : le fournisseur ; copies : l'officine, dont une réponse n'est jamais celle du fournisseur
            adresses = lambda s: [x.strip() for x in re.split(r"[,;]", s or "") if x.strip()]
            tache.metadata.update({"reference": doc["reference"], "commande": doc,
                                   "compte": compte, "nature": nature, "identites": identites(doc), "empreinte": empreinte(doc),
                                   "destinataires": adresses(self.c.destinataire), "copies": adresses(self.c.copie)})
        # La tâche est à son auteur dès sa création : avec un jeton rattaché à un
        # compte, toute tâche (fiche, état de rayon, commande, relance) porte ce compte, sinon son
        # auteur ne pourrait pas la relire
        auth = (getattr(getattr(ctx, "call_context", None), "state", None) or {}).get("auth")
        compte = auth.get("pharma2a.compte") if isinstance(auth, dict) else None
        # L'OFFICINE DE LA TÂCHE EST CELLE DU JETON (revue du 4 novembre) : le compte que le jeton
        # prouve, jamais un compte écrit dans le document ; vide pour le jeton unique d'un agent
        # qui ne sert qu'une officine. Lecture, répétitions et relances ne regardent qu'elle.
        tache.metadata.update({"officine": officine_du_compte(compte)})
        if compte:
            deja_ids = list(MessageToDict(tache.metadata).get("identites") or [])
            tache.metadata.update({"identites": sorted(set(deja_ids) | {"compte:" + normal(compte)}),
                                   "lecteurs": ["compte:" + normal(compte)]})
        await queue.enqueue_event(tache)
        maj = TaskUpdater(queue, tache.id, tache.context_id)
        mot = lambda t: maj.new_agent_message([new_text_part(t)])
        # le guichet a vu un envoi précédent annulé sous la même référence : le mail le dira
        meta_message = dict(message.metadata) if message.metadata else {}
        remplace = (str(meta_message.get("pharma2a.remplace_motif") or "fermee")) if meta_message.get("pharma2a.remplace") else None
        # Un canal ERP n'a pas de mail : envoyer ne se cherche qu'au moment d'écrire
        def envoyer(*a, **k):
            if "message_id" in inspect.signature(self.backend.envoyer).parameters:
                k["message_id"] = message_id
            return self.backend.envoyer(*a, **k)
        if not donnees:
            await maj.reject(mot("Aucune partie de type données : le message doit porter un document du dictionnaire PharmA2A."))
            return
        if meta_message.get("pharma2a.doublon_different"):
            await maj.reject(mot(f"Une tâche est encore ouverte sous la référence {doc.get('reference')} "
                                 f"({meta_message['pharma2a.doublon_different']}) avec un autre contenu : fermez-la (tasks/{{id}}:cancel) "
                                 "puis renvoyez, et le fournisseur saura que ce mail annule et remplace le précédent ; "
                                 "ou envoyez sous une nouvelle référence. Rien n'est parti."))
            return
        if isinstance(doc, dict) and doc.get("demande") == DEMANDE:
            # Le guichet : l'officine demande la fiche, l'agent la rend en artefact. Rien ne part au laboratoire.
            fiche, pourquoi = self.fiche.lire()
            if fiche is None:
                await maj.failed(mot(f"Pas de fiche à rendre : {pourquoi}."))
                return
            await maj.add_artifact([new_data_part(fiche, media_type=MEDIA["fiche_fournisseur"])], name="fiche_fournisseur",
                                   extensions=[EXTENSION])
            await maj.complete(mot(f"Fiche du fournisseur du {fiche['date']}, {len(fiche.get('gamme') or [])} référence(s)."))
            return
        media = next((p.media_type for p in message.parts if p.HasField("data")), None)
        meta = dict(message.metadata) if message.metadata else {}
        nom = detecter(media, meta, doc)
        if nom == "relance":
            erreurs = erreurs_de("relance", doc)
            if erreurs:
                await maj.reject(mot("relance non conforme au dictionnaire PharmA2A : " + " ; ".join(erreurs[:8])))
                return
            if origine is None:
                await maj.reject(mot(f"Aucune commande reçue sous la référence {doc['reference']} pour cette officine : "
                                     "une relance porte sur une commande déjà transmise par cet agent. Rien n'est parti."))
                return
            if deja is not None:
                await maj.reject(mot(f"Cette commande a déjà été relancée aujourd'hui pour ce motif (tâche {deja.id}). Rien n'est parti."))
                return
            await maj.submit(mot(f"Relance de la commande {doc['reference']} reçue."))
            try:
                commande_o = renormaliser(MessageToDict(origine.metadata).get("commande"))
                if hasattr(self.backend, "transmettre"):
                    # l'ERP reçoit la relance comme la commande : le document du dictionnaire
                    envoi = self.backend.transmettre("relance", doc, reference=doc["reference"], message_id=message_id)
                    await maj.start_work(mot(f"Relance de la commande {doc['reference']} transmise au fournisseur ({envoi}). "
                                             "En attente de sa réponse."))
                    return
                objet, texte, html = courrier.relance(doc, commande_o)
                kw = {}
                params = inspect.signature(self.backend.envoyer).parameters
                if "message_id" in params:
                    kw["message_id"] = message_id
                fil = MessageToDict(origine.metadata).get("message_id")
                if fil and "en_reponse_a" in params:
                    kw["en_reponse_a"] = fil
                envoi = self.backend.envoyer(self.c.destinataire, self.c.copie, objet, texte, html,
                                             reference=doc["reference"], nom_affiche=doc["officine"].get("nom"), **kw)
                await maj.start_work(mot(f"Relance de la commande {doc['reference']} transmise au fournisseur ({envoi}), "
                                         "dans le fil de la commande. En attente de sa réponse."))
            except Exception as e:
                log.exception("relance échouée")
                await maj.failed(mot(f"Transmission de la relance échouée : {type(e).__name__} : {str(e)[:200]}"))
            return
        if nom not in ("commande", "etat_de_rayon"):
            await maj.reject(mot("Message inconnu : attendu « commande », « etat_de_rayon », « relance » ou une demande de fiche "
                                 "(type de média ou métadonnée pharma2a.message)."))
            return
        # le schéma d'abord : tant qu'il n'est pas respecté, on ne lit rien dans le document
        erreurs = erreurs_de(nom, doc)
        # l'état de rayon est pour ce fournisseur (un document qui n'est pas un objet est refusé plus bas)
        autre = None if erreurs else rayon_d_un_autre(nom, doc, self.c.fournisseur)
        if autre:
            erreurs.append(autre)
        rayon = doc.get("etat_de_rayon") if (not erreurs and nom == "commande" and isinstance(doc, dict)) else None
        if erreurs:
            await maj.reject(mot(f"{nom} non conforme au dictionnaire PharmA2A : " + " ; ".join(erreurs[:8])))
            return
        await maj.submit(mot(f"{nom} reçu, {len(doc['lignes'])} ligne(s)."))
        if nom == "commande" and self.index is not None:
            # LA BASE REFUSE LE DOUBLON : l'envoi se réserve juste avant de partir ; pour une
            # officine prouvée, un envoi vivant sous la même référence et la même nature l'empêche
            meta_t = MessageToDict(tache.metadata)
            cle = Index.cle(self.c.agent or "", meta_t.get("officine") or "", doc["reference"], meta_t.get("nature") or "commande")
            if not self.index.reserver(cle, tache.id, _adresses_cle(self.c.destinataire)):
                await maj.reject(mot(f"La commande {doc['reference']} est déjà transmise pour ce compte et attend sa suite : "
                                     "rien n'est reparti. Fermez la tâche précédente pour la remplacer, ou envoyez sous une nouvelle référence."))
                return
        try:
            if nom == "commande":
                ref = doc["reference"]
                objet, texte, html = courrier.commande(doc, remplace=remplace)
                pieces = []
                if rayon:
                    # Un seul mail : le bon de commande avec stock et ventes sur chaque ligne, et
                    # l'Excel à deux onglets (Commande, État de rayon) en pièce jointe
                    pieces = [(f"commande-{ref.lower()}-et-etat-de-rayon.xlsx", courrier.etat_de_rayon_xlsx(rayon, doc),
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")]
                if hasattr(self.backend, "transmettre"):
                    # Un ERP reçoit la commande validée, pas un mail : le document du dictionnaire
                    envoi = self.backend.transmettre("commande", doc, reference=ref, message_id=message_id)
                else:
                    envoi = envoyer(self.c.destinataire, self.c.copie, objet, texte, html, pieces=pieces or None,
                                    reference=ref, nom_affiche=(doc.get("officine") or {}).get("nom"))
                if hasattr(self.backend, "ecrire_contenu"):
                    self.backend.ecrire_contenu(ref, nom, doc)
                await maj.start_work(mot(f"Commande {ref} transmise au fournisseur ({envoi})"
                                         + (", en remplacement de l'envoi précédent fermé sous la même référence" if remplace == "fermee"
                                            else ", à la suite de la proposition envoyée sous la même référence" if remplace == "proposition"
                                            else ", après un envoi précédent dont l'issue était incertaine, qu'il remplace s'il est arrivé" if remplace else "")
                                         + (f", avec l'état de rayon ({len(rayon['lignes'])} références)" if rayon else "")
                                         + ". En attente de sa réponse."))
                # la tâche reste « working » jusqu'à la lecture de la réponse du fournisseur
            else:
                objet, texte, html, fichier = courrier.etat_de_rayon(doc)
                piece = (fichier, courrier.etat_de_rayon_xlsx(doc),
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                ref = f"RAYON-{doc['fournisseur']}-{doc['date']}"
                if hasattr(self.backend, "transmettre"):
                    envoi = self.backend.transmettre("etat_de_rayon", doc, reference=ref, message_id=message_id)
                else:
                    envoi = envoyer(self.c.destinataire, self.c.copie, objet, texte, html, pieces=[piece], reference=ref,
                                    nom_affiche=(doc.get("officine") or {}).get("nom"))
                if hasattr(self.backend, "ecrire_contenu"):
                    self.backend.ecrire_contenu(ref, nom, doc)
                await maj.complete(mot(f"État de rayon transmis au fournisseur ({envoi}), {len(doc['lignes'])} référence(s)."))
        except Exception as e:
            # UN ENVOI INCERTAIN NE SE RETENTE JAMAIS TOUT SEUL (idée reprise du pont de référence) :
            # le canal a levé une erreur, mais le serveur a pu accepter la commande avant. La tâche
            # le dit et attend une décision : vérifier chez le fournisseur, puis fermer la tâche
            # pour renvoyer (le mail dira alors qu'il annule et remplace). L'envoi reste réservé :
            # la même commande ne peut pas repartir en double pendant ce temps.
            log.exception("transmission incertaine")
            await maj.update_status(TaskState.TASK_STATE_INPUT_REQUIRED, metadata={"envoi_incertain": True}, message=mot(f"Issue de l'envoi incertaine ({type(e).__name__} : {str(e)[:160]}) : le fournisseur a "
                                         "peut-être reçu la commande. Vérifiez auprès de lui ; pour renvoyer, fermez cette tâche, "
                                         "le nouvel envoi dira qu'il annule et remplace celui-ci."))

    async def cancel(self, ctx: RequestContext, queue: EventQueue) -> None:
        # Fermer n'est pas rappeler : la tâche se ferme à la demande de l'officine, quand elle a
        # tranché ou remplacé la commande ; le mail parti ne revient pas. L'envoi se ferme dans
        # le registre : la même référence pourra repartir, en annulant et remplaçant.
        if self.index is not None:
            self.index.fermer(ctx.task_id)
        maj = TaskUpdater(queue, ctx.task_id, ctx.context_id)
        await maj.cancel(maj.new_agent_message([new_text_part(
            "Fermée par l'officine. Fermer la tâche ne rappelle pas le mail : si une commande est partie et "
            "doit être annulée, prévenez le fournisseur.")]))
