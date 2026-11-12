"""La carte d'agent : nom, description, adresse du fournisseur, interfaces, sécurité et
compétences, publiques. La carte étendue, identique, reste servie aux clients authentifiés."""
from __future__ import annotations
from a2a.types import (AgentCapabilities, AgentCard, AgentExtension, AgentInterface, AgentSkill, AgentProvider,
                       HTTPAuthSecurityScheme, SecurityRequirement, SecurityScheme, StringList)
from . import __version__
from .dictionnaire import EXTENSION, MEDIA, NOMS

COMPETENCES = [
    AgentSkill(
        id="commander", name="Commander",
        description="Reçoit un bon de commande (message « commande » du dictionnaire PharmA2A, sans aucun prix), "
                    "le transmet au service commandes du fournisseur, et rapporte l'accusé de réception "
                    "(« accuse_reception ») en artefact. Reliquat, rupture ou substitution : la tâche demande l'avis du pharmacien. "
                    "Une même référence pour un même compte client n'est transmise qu'une fois.",
        tags=["commande", "officine", "fournisseur", "laboratoire", "pharma2a"],
        input_modes=[MEDIA["commande"]], output_modes=[MEDIA["accuse_reception"]],
        examples=["Commande BC-26018-EXEMPLE, 3 lignes, pour la Pharmacie de l'Exemple"]),
    AgentSkill(
        id="etat_de_rayon", name="État de rayon",
        description="Reçoit l'état de rayon que l'officine partage sur les seules références du fournisseur "
                    "(stock, ventes 3 mois, couverture, mois à zéro ; jamais de prix ni de marge) et le transmet au fournisseur.",
        tags=["rayon", "stock", "officine", "fournisseur", "pharma2a"],
        input_modes=[MEDIA["etat_de_rayon"]], output_modes=["text/plain"]),
    AgentSkill(
        id="relancer", name="Relancer",
        description="Reçoit une relance (« relance ») sur une commande déjà transmise sous la même référence pour la même officine : "
                    "livraison annoncée non arrivée, ou commande sans réponse. La transmet au fournisseur dans le fil de la commande, "
                    "et rapporte sa réponse (« accuse_reception »). Une relance ne passe jamais de nouvelle commande.",
        tags=["relance", "livraison", "commande", "fournisseur", "pharma2a"],
        input_modes=[MEDIA["relance"]], output_modes=[MEDIA["accuse_reception"]],
        examples=["Relance de la commande BC-26018-EXEMPLE : livraison annoncée le 30/10, non arrivée"]),
    AgentSkill(
        id="fiche", name="Fiche du fournisseur",
        description="Rend la fiche du fournisseur (« fiche_fournisseur ») en artefact : qui il est, comment on le joint, "
                    "ses conditions générales, sa gamme par CIP 13 avec colisage, statut et, s'il le publie, son tarif catalogue. "
                    "La même fiche se lit sans clé sur /fiche_fournisseur.json, sans le tarif sauf si le fournisseur le rend public.",
        tags=["fiche", "catalogue", "conditions", "fournisseur", "pharma2a"],
        input_modes=["application/json"], output_modes=[MEDIA["fiche_fournisseur"]],
        examples=['{"version": "1", "demande": "fiche_fournisseur"}']),
]


# L'extension PharmA2A : la carte dit, dans la langue d'A2A, que cet agent parle le
# dictionnaire. Facultative : un client qui ne la demande pas est servi pareil, le dictionnaire
# s'impose de toute façon à chaque message (un message hors schéma est refusé).
EXTENSION_PHARMA2A = AgentExtension(
    uri=EXTENSION, required=False,
    description="PharmA2A, le dictionnaire de la commande directe entre l'officine et ses fournisseurs : "
                "chaque message est une partie de données conforme à l'un de ses schémas JSON.",
    params={"dictionnaire": "1", "messages": list(NOMS),
            "schemas": "https://github.com/pharma2a/pharma2a/tree/main/schema",
            "specification": EXTENSION})


def cartes(config) -> tuple[AgentCard, AgentCard]:
    """(publique, étendue)."""
    base = dict(
        name=f"Agent PharmA2A ({config.fournisseur})",
        description="Agent de fournisseur PharmA2A : reçoit les commandes directes et les états de rayon d'une officine, "
                    "les transmet au fournisseur, rapporte ses réponses. Il ne décide rien, il transcrit. Aucun prix de l'officine ne circule ; "
                    "seules les conditions du fournisseur (tarif, remise, prix net, franco), telles qu'il les donne.",
        version=__version__,
        # le texte d'A2A veut l'adresse du fournisseur : son site, à défaut celle de l'agent
        provider=AgentProvider(organization=config.fournisseur, url=config.site or config.url),
        supported_interfaces=[
            AgentInterface(url=config.url, protocol_binding="HTTP+JSON", protocol_version="1.0"),
            AgentInterface(url=config.url, protocol_binding="JSONRPC", protocol_version="1.0"),
        ],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False,
                                       extended_agent_card=not config.tout_public,
                                       extensions=[EXTENSION_PHARMA2A]),
        security_schemes={"bearer": SecurityScheme(http_auth_security_scheme=HTTPAuthSecurityScheme(
            scheme="bearer", bearer_format="opaque",
            description="Jeton partagé, un par couple officine et fournisseur, remis hors ligne."))},
        security_requirements=[SecurityRequirement(schemes={"bearer": StringList(list=[])})],
        default_input_modes=["application/json"], default_output_modes=["application/json"],
    )
    # Les compétences sont publiques : le texte d'A2A les exige dans la carte, et elles ne
    # disent rien que la fiche ne dise déjà ; la carte étendue reste, identique, pour les
    # clients qui la demandent (PHARMA2A_TOUT_PUBLIC=1 ne l'annonce plus)
    publique = AgentCard(**base, skills=COMPETENCES)
    etendue = AgentCard(**base, skills=COMPETENCES)
    return publique, etendue
