"""L'application A2A : la carte, les routes HTTP+JSON et JSON-RPC du SDK, et la porte
(le jeton porteur) devant tout, sauf la carte publique."""
from __future__ import annotations
import base64
import functools
import hmac
import inspect
import time
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Mount
from starlette.types import ASGIApp, Receive, Scope, Send
from a2a.helpers import get_data_parts
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.types import ListTasksRequest, ListTasksResponse, Role, TaskState
from a2a.utils.constants import DEFAULT_LIST_TASKS_PAGE_SIZE
from a2a.utils.errors import InvalidParamsError, TaskNotFoundError, VersionNotSupportedError
from a2a.utils.task import apply_history_length, validate_history_length, validate_page_size
from a2a.server.routes import create_jsonrpc_routes, create_rest_routes
from a2a.server.tasks import DatabaseTaskStore, InMemoryTaskStore
from .carte import cartes
from .carte_route import route_carte, CARTE
from .fiche import Fiche, route_fiche, FICHE
from .dictionnaire import cle_commande, empreinte, erreurs_de, identites, reference_de, renormaliser
from .jetons import Registre, normal, officine_de, officine_du_compte
from .index_commandes import Index
from .executeur import Executeur, rayon_d_un_autre
from . import backends, extraction, relecture
from .annuaire import Annuaire
import asyncio
import contextlib
import logging
from google.protobuf.json_format import MessageToDict


class Porte:
    """Authorization: Bearer <jeton> sur toute route, sauf la carte publique et la fiche du
    fournisseur (GET/HEAD) : la vitrine se lit sans clé. Le jeton est celui d'une officine,
    pris au registre (PHARMA2A_JETONS), ou le jeton seul de l'agent (PHARMA2A_JETON). Le
    compte client auquel il est rattaché suit la requête (scope « auth ») jusqu'au guichet.
    La carte étendue passe par cette porte : elle n'est servie qu'à un client authentifié."""

    def __init__(self, app: ASGIApp, jeton: str = "", compte_client: str = "", registre: Registre | None = None):
        self.app, self.jeton, self.compte_client, self.registre = app, jeton, compte_client, registre

    def _compte(self, jeton: str):
        """(reconnu, compte) : le compte client du jeton, None s'il vaut pour tout compte."""
        if self.jeton and hmac.compare_digest(jeton.encode("utf-8"), self.jeton.encode("utf-8")):
            return True, (self.compte_client or None)
        if self.registre is not None:
            compte = self.registre.compte_de(jeton)
            if compte:
                return True, compte
        return False, None

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope["path"] in (CARTE, FICHE) and scope["method"] in ("GET", "HEAD"):
            return await self.app(scope, receive, send)
        entete = dict(scope.get("headers") or {}).get(b"authorization", b"").decode("latin-1")
        schema, _, jeton = entete.partition(" ")
        reconnu, compte = (self._compte(jeton.strip()) if schema.lower() == "bearer" and jeton.strip() else (False, None))
        if not reconnu:
            reponse = JSONResponse({"erreur": "jeton porteur absent ou invalide"}, status_code=401,
                                   headers={"WWW-Authenticate": 'Bearer realm="pharma2a"'})
            return await reponse(scope, receive, send)
        scope = dict(scope)
        scope["auth"] = {"pharma2a.compte": compte}
        return await self.app(scope, receive, send)


def compte_du_jeton(context) -> str | None:
    """Le compte client du jeton qui appelle, ou None (jeton qui vaut pour tout compte)."""
    auth = (getattr(context, "state", None) or {}).get("auth")
    return auth.get("pharma2a.compte") if isinstance(auth, dict) else None


class TypeDeContenu:
    """Le type de la réponse se négocie. Le texte d'A2A 1.0 se contredit : la liaison HTTP+JSON
    exige `application/json` (section 11.1), ses exemples et son enregistrement de type de
    média disent `application/a2a+json`, que le SDK officiel envoie. Le client choisit : celui
    qui annonce `application/a2a+json` dans Accept le reçoit, les autres reçoivent
    `application/json`. Le corps est le même."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        accepte = dict(scope.get("headers") or {}).get(b"accept", b"").decode("latin-1").lower()
        if "application/a2a+json" in accepte:
            return await self.app(scope, receive, send)

        async def envoyer(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = [(k, b"application/json" if k.lower() == b"content-type" and v.lower().startswith(b"application/a2a+json") else v)
                                      for k, v in message.get("headers") or []]
            await send(message)
        return await self.app(scope, receive, envoyer)


# une tâche refusée, échouée ou annulée n'a rien transmis : la même référence peut revenir
SANS_SUITE = {TaskState.TASK_STATE_REJECTED, TaskState.TASK_STATE_FAILED, TaskState.TASK_STATE_CANCELED}


def est_a(meta: dict, agent: str) -> bool:
    """La tâche est à cet agent : même identifiant d'agent, rien d'autre. Une tâche sans agent
    noté n'est à personne dans un annuaire ; un agent seul (identifiant vide) garde les siennes."""
    return str(meta.get("agent") or "") == str(agent or "")


# un seul verrou par clé de commande pour tout le processus : il survit aux instances du
# guichet, que l'annuaire rebâtit quand son fichier change
VERROUS: dict[tuple, asyncio.Lock] = {}


async def commande_deja_recue(store, agent, doc, ctx, compte_jeton: str | None = None, index=None):
    """(vivante, précédente) : la tâche déjà ouverte pour cette commande (même agent, même compte,
    même référence, même nature), et à défaut une tâche fermée sous la même clé dont l'envoi a
    pu partir : annulée par l'officine, ou échouée (le canal a levé une erreur, mais le mail
    a pu être accepté avant). Une tâche refusée n'a rien envoyé. On parcourt le magasin : les
    tâches portent leur clé en métadonnée."""
    compte, ref, nature = cle_commande(doc)
    ids = identites(doc)
    if not ref or not ids:
        return None, None
    # LE MAGASIN PAGINE PAR DATE DE DERNIER CHANGEMENT (revue du 4 novembre) : une tâche mise à
    # jour entre deux pages (un accusé qui arrive) remonte avant le curseur et ne se voit sur
    # aucune page. Une commande déjà transmise passerait pour neuve et repartirait. Le magasin
    # est donc relu jusqu'à ce que deux lectures complètes rendent la même chose : une tâche
    # vue une fois n'est jamais perdue, et rien ne repart sur un doute.
    vues: dict = {}
    for _ in range(5):
        avant = set(vues)
        jeton = ""
        while True:
            rep = await store.list(ListTasksRequest(page_size=100, page_token=jeton, include_artifacts=False), ctx)
            for t in rep.tasks:
                vues[t.id] = t
            jeton = rep.next_page_token
            if not jeton:
                break
        if _ and set(vues) == avant:
            break
    # l'index mène tout droit aux tâches de cette clé : même manquées par le parcours, elles sont relues
    if index is not None:
        for nat in {nature, "proposition"}:
            for tid in index.taches(Index.cle(agent, officine_du_compte(compte_jeton), ref, nat)):
                t = await store.get(tid, ctx)
                if t is not None:
                    vues[t.id] = t
    taches = list(vues.values())
    deja, autre = _choisir(taches, agent, ref, nature, ids, compte_jeton)
    if deja is None and _deja_sous_autre_mode(taches, agent, ref, nature, ids, compte_jeton):
        # LE CHANGEMENT DE MODE DE JETON NE RENVOIE PAS UNE COMMANDE (revues des 4 et 5 novembre) :
        # une commande passée sous un autre jeton (unique ou du registre) porte une autre officine ;
        # si une identité quelconque (compte, CIP, adresse) la relie à celle-ci, la même référence
        # ne repart pas, et l'ancienne tâche n'est pas montrée pour autant
        raise InvalidParamsError(f"la commande {ref} a déjà été reçue pour cette officine sous un autre jeton, avant le "
                                 "changement de jeton : rien n'est reparti ; envoyez sous une nouvelle référence s'il s'agit "
                                 "d'une autre commande")
    return deja, autre


def _deja_sous_autre_mode(taches, agent, ref, nature, ids, compte_jeton) -> bool:
    officine = officine_du_compte(compte_jeton)
    for t in taches:
        meta = MessageToDict(t.metadata) if t.HasField("metadata") else {}
        # seulement entre les deux modes : le jeton unique (officine vide) d'un côté, le registre de l'autre
        if not (est_a(meta, agent) and not meta.get("suite_de") and (officine_de(meta) == "") != (officine == "")
                and reference_de(meta).strip().upper() == ref
                and str(meta.get("nature") or "commande") == nature
                and t.status.state not in SANS_SUITE):
            continue
        ids2 = set(meta.get("identites") or []) | {officine_de(meta)} - {""}
        if isinstance(meta.get("commande"), dict):
            ids2 |= set(identites(renormaliser(meta["commande"])))
        if ids2 & set(ids):
            return True
    return False


def _choisir(taches, agent, ref, nature, ids, compte_jeton):
    annulee, precedente = None, None
    for t in sorted(taches, key=lambda x: x.status.timestamp.ToDatetime(), reverse=True):
        if not t.HasField("metadata"):
            continue
        meta = MessageToDict(t.metadata)
        # une tâche de suite (second mail) n'est pas une commande reçue : la première l'est
        if not est_a(meta, agent) or meta.get("suite_de"):
            continue
        r2 = reference_de(meta).strip().upper()
        n2 = str(meta.get("nature") or (meta.get("commande") or {}).get("nature") or "commande")
        if r2 != ref:
            continue
        # la même officine : une identité en commun (compte, CIP, nom et adresse), lues sur
        # la commande que la tâche porte ; une tâche d'avant les identités n'a que son compte
        ids2 = set(meta.get("identites") or []) | (set(identites(renormaliser(meta["commande"]))) if meta.get("commande") else set())
        ids2 |= {str(meta.get("compte") or "")} - {""}
        # Un jeton rattaché ne voit que son compte, avant tout rapprochement :
        # une officine qui recopie le CIP et la référence d'une autre ne retrouve ni sa tâche ni
        # sa proposition, et ne s'y fait pas ajouter
        # La répétition se cherche dans l'officine du jeton, et seulement là : une officine qui
        # recopie le CIP et la référence d'une autre ne retrouve ni sa tâche ni sa proposition.
        # Sous le jeton unique (une seule officine), les identités du document départagent.
        if officine_de(meta) != officine_du_compte(compte_jeton):
            continue
        if not compte_jeton and not (ids2 & set(ids)):
            continue
        if n2 != nature:
            # la commande ferme qui suit la proposition, sous la même référence et pour la même
            # officine : le mail dira qu'elle fait suite à la proposition (quel que soit son état)
            if nature == "commande" and n2 == "proposition" and t.status.state != TaskState.TASK_STATE_REJECTED and precedente is None:
                precedente = t
            continue
        if t.status.state in (TaskState.TASK_STATE_CANCELED, TaskState.TASK_STATE_FAILED):
            annulee = t
        elif t.status.state not in SANS_SUITE:
            # rien ne s'apprend ici : le guichet ne retient les identités qu'une fois la
            # répétition acceptée (apprendre_identites), jamais sur une correction refusée
            return t, None
    return None, (annulee or precedente)


def lecteurs_de(meta: dict) -> set:
    """Les comptes qui peuvent lire la tâche : ceux qu'un jeton a prouvés (lecteurs). Une tâche
    d'avant cette distinction n'a que ses identités, qui en tiennent lieu."""
    if "lecteurs" in meta:
        return set(meta.get("lecteurs") or [])
    return {i for i in meta.get("identites") or [] if str(i).startswith("compte:")}


async def apprendre_identites(store, t, doc, ctx, compte_jeton: str | None):
    """LA RÉPÉTITION ACCEPTÉE APPREND (revue du 4 novembre : reconnaître n'est pas autoriser).
    La tâche retient les identités nouvelles de la commande répétée, pour les rapprochements
    suivants (identites). La lecture, elle, ne s'ouvre qu'aux comptes qu'un jeton a prouvés
    (lecteurs) : un document qui se contente de nommer un compte n'ouvre rien. Relue juste
    avant d'écrire, pour ne pas écraser un accusé posé entre-temps."""
    meta = MessageToDict(t.metadata) if t.HasField("metadata") else {}
    autorise = ("compte:" + normal(compte_jeton)) if compte_jeton else None
    nouvelles = set(identites(doc)) - set(meta.get("identites") or [])
    lecteur = autorise and autorise not in set(meta.get("lecteurs") or [])
    if not nouvelles and not lecteur:
        return t
    async with relecture.verrou_tache(t.id):
        courant = await store.get(t.id, ctx)
        if courant is None:
            return t
        m = MessageToDict(courant.metadata) if courant.HasField("metadata") else {}
        courant.metadata["identites"] = sorted(set(m.get("identites") or []) | nouvelles)
        if lecteur:
            courant.metadata["lecteurs"] = sorted(set(m.get("lecteurs") or []) | {autorise})
        await store.save(courant, ctx)
        return courant


class Guichet(DefaultRequestHandler):
    """Une commande n'est transmise qu'une fois : la même référence pour le même compte client,
    renvoyée après une coupure réseau, rend la tâche déjà ouverte (même identifiant, même état)
    au lieu d'en créer une seconde. Rien ne repart au fournisseur. Deux envois simultanés de la
    même clé passent l'un après l'autre (un verrou par clé). Et chaque agent de l'annuaire ne
    voit que ses tâches, bien que le magasin soit partagé."""

    def __init__(self, *args, agent: str = "", fournisseur: str = "", index: Index | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.agent = agent
        self.fournisseur = fournisseur
        self.index = index

    def _mienne(self, tache, context=None) -> bool:
        """Dans un annuaire, une tâche est à l'agent qu'elle nomme ; sans métadonnée, à personne.
        Et pour un jeton rattaché à un compte client, elle est à l'officine de ce compte : une
        officine ne voit pas les tâches d'une autre."""
        if tache is None:
            return True
        meta = MessageToDict(tache.metadata) if tache.HasField("metadata") else None
        # toujours, même pour l'agent seul : une base reprise d'un annuaire garde les tâches
        # d'autres fournisseurs, que cet agent ne lit pas (revue du 5 novembre)
        if self.agent and meta is None:
            return False
        if not est_a(meta or {}, self.agent):
            return False
        # une tâche se lit par l'officine du jeton qui l'a créée, et par elle seule
        return officine_de(meta or {}) == officine_du_compte(compte_du_jeton(context))

    async def _verifier_la_tache(self, identifiant, context):
        tache = await self.task_store.get(identifiant, context)
        if tache is not None and not self._mienne(tache, context):
            raise TaskNotFoundError(f"Task {identifiant} not found")

    async def on_message_send(self, params, context):
        message = params.message
        if message and message.task_id:
            await self._verifier_la_tache(message.task_id, context)
            async with relecture.verrou_tache(message.task_id):
                await self._rafraichir(message.task_id)
                return await super().on_message_send(params, context)
        donnees = get_data_parts(message.parts) if message else []
        doc = renormaliser(donnees[0]) if donnees else None
        # ce que le guichet dit à l'exécuteur ne vient que du guichet : un client ne pose pas ces clés
        if message and message.metadata:
            for cle_client in [k for k in message.metadata if str(k).startswith("pharma2a.remplace") or str(k).startswith("pharma2a.doublon")]:
                del message.metadata[cle_client]
        # Le compte du jeton : une officine ne commande que pour son propre compte client
        compte = compte_du_jeton(context)
        if compte and isinstance(doc, dict) and isinstance(doc.get("officine"), dict):
            porte = doc["officine"].get("numero_compte_client")
            if normal(porte) != normal(compte):
                logging.getLogger("pharma2a").warning("document refusé : compte client %r, jeton du compte %r", porte, compte)
                raise InvalidParamsError("le numéro de compte client du document n'est pas celui du jeton présenté")
        if not (isinstance(doc, dict) and reference_de(doc) and doc.get("officine") and doc.get("lignes")):
            return await super().on_message_send(params, context)
        # L'enveloppe A2A avant tout rapprochement : une répétition rendue
        # directement ne passait pas par les contrôles de la méthode parente
        if not message.message_id or message.role == Role.ROLE_UNSPECIFIED:
            raise InvalidParamsError("message incomplet : messageId et role sont obligatoires (A2A 1.0, section 5.7)")
        validate_history_length(params.configuration)
        # Une répétition se reconnaît conforme : un document hors dictionnaire va à l'exécuteur,
        # qui le refuse, sans rien apprendre à la tâche existante
        if erreurs_de("commande", doc):
            return await super().on_message_send(params, context)
        # de même un état de rayon au nom d'un autre fournisseur : l'exécuteur le refuse, la tâche
        # existante n'est pas rendue comme si l'envoi était accepté
        if rayon_d_un_autre("commande", doc, self.fournisseur):
            return await super().on_message_send(params, context)
        cle = (self.agent,) + cle_commande(doc)[1:]      # référence et nature : l'identité se compare, elle ne se verrouille pas
        async with VERROUS.setdefault(cle, asyncio.Lock()):
            deja, annulee = await commande_deja_recue(self.task_store, self.agent, doc, context, compte_du_jeton(context), self.index)
            if deja is not None:
                meta_deja = MessageToDict(deja.metadata) if deja.HasField("metadata") else {}
                # recalculée sur la commande gardée : une empreinte d'une version d'avant ne se compare pas
                avant = empreinte(renormaliser(meta_deja["commande"])) if meta_deja.get("commande") else meta_deja.get("empreinte")
                if avant is not None and avant != empreinte(doc):
                    # Même référence, autre contenu, tâche encore ouverte : ce n'est pas une
                    # répétition, et rendre l'ancienne tâche en silence ferait croire à
                    # l'officine que sa correction est partie ; l'exécuteur refuse et dit quoi faire
                    logging.getLogger("pharma2a").info("commande %s reçue avec un autre contenu que la tâche ouverte %s : refusée", reference_de(doc), deja.id)
                    message.metadata.update({"pharma2a.doublon_different": deja.id})
                    return await super().on_message_send(params, context)
                deja = await apprendre_identites(self.task_store, deja, doc, context, compte_du_jeton(context))
                logging.getLogger("pharma2a").info("commande %s déjà reçue : tâche %s rendue telle quelle", reference_de(doc), deja.id)
                # la limite d'historique demandée vaut aussi pour la tâche rendue
                validate_history_length(params.configuration)
                return apply_history_length(deja, params.configuration)
            if annulee is not None:
                # La référence revient après une fermeture ou un échec : c'est le geste d'une
                # officine qui corrige ou qui réessaie ; on le laisse passer, et le fournisseur est
                # prévenu que ce mail annule et remplace le précédent, qu'il l'ait reçu ou non
                # (l'exécuteur le lit dans la métadonnée du message)
                logging.getLogger("pharma2a").info("commande %s renvoyée après la tâche %s (%s)", reference_de(doc), annulee.id, annulee.status.state)
                meta_annulee = MessageToDict(annulee.metadata) if annulee.HasField("metadata") else {}
                nature_annulee = str(meta_annulee.get("nature") or (meta_annulee.get("commande") or {}).get("nature") or "commande")
                incertain = bool(meta_annulee.get("envoi_incertain")) or annulee.status.state == TaskState.TASK_STATE_FAILED
                motif = ("proposition" if (nature_annulee == "proposition" and cle_commande(doc)[2] == "commande")
                         else "incertain" if incertain else "fermee")
                message.metadata.update({"pharma2a.remplace": annulee.id, "pharma2a.remplace_motif": motif})
            return await super().on_message_send(params, context)

    async def on_get_task(self, params, context):
        tache = await super().on_get_task(params, context)
        if not self._mienne(tache, context):
            raise TaskNotFoundError(f"Task {params.id} not found")
        return tache

    async def _rafraichir(self, tache_id):
        """La copie en mémoire se relit : l'exécution d'une tâche garde en
        mémoire la tâche telle qu'elle l'a laissée (« working »). La relecture écrit l'accusé
        dans le magasin sans passer par elle ; annuler repartait de la vieille copie et
        effaçait l'accusé. On la fait relire au magasin avant d'agir."""
        active = await self._active_task_registry.get(tache_id)
        gestionnaire = getattr(active, "_task_manager", None) if active is not None else None
        if gestionnaire is not None:
            gestionnaire.invalidate()

    async def on_cancel_task(self, params, context):
        await self._verifier_la_tache(params.id, context)
        # l'annulation et la relecture d'une réponse ne s'écrivent pas l'une sur l'autre
        async with relecture.verrou_tache(params.id):
            await self._rafraichir(params.id)
            return await super().on_cancel_task(params, context)

    async def on_list_tasks(self, params, context):
        """La liste se filtre avant de se compter : filtrée après la
        pagination, une officine sans tâche recevait une page vide et un total qui comptait
        celles des autres. On parcourt tout le magasin avec les filtres de la requête, on ne
        garde que les tâches de l'appelant, puis on compte et on découpe la page."""
        # toujours filtrée, même sous le jeton unique : après un changement de mode de jeton, la
        # base garde les tâches des anciens comptes, que la liste ne doit pas rendre (revue du 4/11)
        if params.HasField("page_size"):
            validate_page_size(params.page_size)
        taille = params.page_size or DEFAULT_LIST_TASKS_PAGE_SIZE
        siennes, jeton = [], ""
        while True:
            req = ListTasksRequest()
            req.CopyFrom(params)
            req.page_size, req.page_token = 100, jeton
            page = await super().on_list_tasks(req, context)
            siennes += [t for t in page.tasks if self._mienne(t, context)]
            jeton = page.next_page_token
            if not jeton:
                break
        # Le curseur garde la position, pas la tâche : une tâche d'ancrage qui
        # change d'état ou de date ne fait ni perdre ni répéter la suite
        siennes.sort(key=_cle_de_tri, reverse=True)
        debut = 0
        if params.page_token:
            apres = _curseur(params.page_token)
            debut = next((i for i, t in enumerate(siennes) if _cle_de_tri(t) < apres), len(siennes))
        morceau = siennes[debut:debut + taille]
        suivant = _jeton_de_page(_cle_de_tri(morceau[-1])) if morceau and debut + taille < len(siennes) else ""
        return ListTasksResponse(tasks=morceau, total_size=len(siennes), page_size=taille, next_page_token=suivant)


def _cle_de_tri(t) -> tuple[int, str]:
    """La plus récente d'abord, l'identifiant départage."""
    return (t.status.timestamp.ToNanoseconds() if t.status.HasField("timestamp") else 0, t.id)


def _jeton_de_page(cle: tuple[int, str]) -> str:
    return base64.urlsafe_b64encode(f"p2a:{cle[0]}:{cle[1]}".encode("utf-8")).decode("ascii")


def _curseur(jeton: str) -> tuple[int, str]:
    try:
        brut = base64.urlsafe_b64decode(jeton.encode("ascii")).decode("utf-8")
        prefixe, ns, tid = brut.split(":", 2)
        if prefixe != "p2a":
            raise ValueError
        return int(ns), tid
    except Exception:
        raise InvalidParamsError("page_token illisible") from None


VERSIONS = ("1.0",)


def version_demandee(context) -> str | None:
    """L'en-tête A2A-Version de la requête, tel quel, ou None s'il n'y en a pas."""
    entetes = (getattr(context, "state", None) or {}).get("headers") or {}
    for k, v in entetes.items():
        if str(k).lower() == "a2a-version" and str(v).strip():
            return str(v).strip()
    return None


def controler_version(context) -> None:
    """Majeure et mineure (A2A 3.6.2) : la bibliothèque ne compare que la
    majeure et laissait passer « 1.999 » pour « 1.0 ». Le correctif est ignoré (1.0.1 = 1.0).
    Sans en-tête, la bibliothèque lit 0.3 et refuse déjà."""
    v = version_demandee(context)
    if v is None:
        return
    morceaux = v.split(".")
    if len(morceaux) < 2 or ".".join(morceaux[:2]) not in VERSIONS or not all(m.isdigit() for m in morceaux):
        raise VersionNotSupportedError(message=f"A2A version '{v}' is not supported. Supported versions: {', '.join(VERSIONS)}.")


def _versionnee(f):
    """Le contrôle de version devant une méthode du guichet, coroutine ou générateur. Les méthodes
    de flux du SDK sont décorées : ce ne sont pas des coroutines, elles rendent un itérateur."""
    if not inspect.iscoroutinefunction(f):
        @functools.wraps(f)
        async def gen(self, params, context=None, *a, **k):
            controler_version(context)
            async for x in f(self, params, context, *a, **k):
                yield x
        return gen

    @functools.wraps(f)
    async def co(self, *a, **k):
        controler_version(k["context"] if "context" in k else (a[1] if len(a) > 1 else None))
        return await f(self, *a, **k)
    return co


for _nom in [n for n in dir(DefaultRequestHandler) if n.startswith("on_")]:
    setattr(Guichet, _nom, _versionnee(getattr(Guichet, _nom)))


def magasin(config):
    if config.base:
        from sqlalchemy.ext.asyncio import create_async_engine
        return DatabaseTaskStore(create_async_engine(f"sqlite+aiosqlite:///{config.base}"))
    return InMemoryTaskStore()


# un registre par base, partagé par les agents d'un annuaire qui partagent le magasin ; sans base,
# un registre en mémoire porté par le magasin lui-même (jamais par son adresse mémoire, qu'un
# autre magasin peut recevoir après lui)
INDEX: dict[str, Index] = {}


def index_pour(config, store) -> Index:
    if config.base:
        return INDEX.setdefault(config.base, Index(config.base))
    idx = getattr(store, "_pharma2a_index", None)
    if idx is None:
        idx = Index(None)
        store._pharma2a_index = idx
    return idx


def app_agent(config, backend, store) -> Starlette:
    """Un agent : sa carte, ses routes, sa porte. Sans cycle de vie : il est porté par la racine."""
    publique, etendue = cartes(config)
    fiche = Fiche(config.fiche or None)
    index = index_pour(config, store)
    handler = Guichet(
        agent_executor=Executeur(config, backend, fiche, store, index),
        task_store=store, agent_card=publique, extended_agent_card=etendue, agent=config.agent or "",
        fournisseur=config.fournisseur or "", index=index)
    routes = ([route_carte(publique, modifie_le=time.time()), route_fiche(fiche, tarif_public=config.fiche_tarif_public)]
              + create_rest_routes(handler) + create_jsonrpc_routes(handler, "/"))
    registre = Registre(config.jetons) if config.jetons else None
    return Starlette(routes=routes, middleware=[Middleware(TypeDeContenu),
                                                Middleware(Porte, jeton=config.jeton, compte_client=config.compte_client, registre=registre)])


def creer_app(config, backend=None, extracteur=None, store=None) -> Starlette:
    manque = config.verifier()
    if manque:
        raise SystemExit("PharmA2A ne démarre pas, il manque : " + " ; ".join(manque))
    backend = backend or backends.charger(config)
    store = store or magasin(config)

    async def passe_relecture():
        ext = extracteur or extraction.charger(config)
        return await relecture.relire(config, store, backend, ext, index=index_pour(config, store))

    @contextlib.asynccontextmanager
    async def vie(app):
        # La relecture : toutes les N minutes, l'agent relit la boîte de réponse du labo.
        # Pas de push, pas de flux : il lit, il transcrit, il met la tâche à jour.
        async def boucle():
            while True:
                await asyncio.sleep(config.relecture_minutes * 60)
                try:
                    n = await passe_relecture()
                    if n:
                        logging.getLogger("pharma2a").info("relecture : %s tâche(s) mise(s) à jour", n)
                except Exception:
                    logging.getLogger("pharma2a").exception("relecture échouée")
        t = asyncio.create_task(boucle()) if config.relecture_minutes > 0 else None
        try:
            yield
        finally:
            if t:
                t.cancel()

    if config.annuaire:
        # plusieurs agents, un par chemin ; chacun a son jeton et son destinataire, le
        # courrier, le magasin et la relecture sont partagés
        def fabrique(c):
            return app_agent(c, type(backend)(c) if hasattr(backend, "c") else backend, store)
        racine = Annuaire(config, fabrique, config.annuaire)
        app = Starlette(routes=[Mount("/", app=racine)], lifespan=vie)
        app.state.annuaire = racine
    else:
        seul = app_agent(config, backend, store)
        app = Starlette(routes=[Mount("/", app=seul)], lifespan=vie)
    app.state.relire = passe_relecture     # pour les tests et « python -m pharma2a --relire »
    app.state.store = store
    return app
