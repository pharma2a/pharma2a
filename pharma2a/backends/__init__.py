"""Le canal vers le fournisseur. Un seul par agent, choisi par PHARMA2A_BACKEND.

« mail » est fourni : il rend la commande en mail (envoyer) et lit les réponses humaines,
qu'un modèle transcrit (lire_reponses). Un fournisseur qui branche son ERP écrit
backends/<nom>.py avec une classe Backend(config) qui expose :

  origine = "erp"
  transmettre(message, doc, reference, message_id) -> str
      reçoit le document validé par l'agent (« commande », avec son état de rayon s'il est
      joint, ou « etat_de_rayon ») et rend le numéro que l'ERP lui donne ;
  lire_reponses(depuis=None) -> list[Reponse]
      chaque Reponse porte `accuse` : l'accusé de réception du dictionnaire, ligne par
      ligne avec l'id_ligne de la commande ; `reference` (la référence de la commande) et
      `en_reponse_a` ([message_id]) disent à quelle commande il répond ;
  acquitter(identifiants) -> None
      ces réponses sont traitées.

L'agent valide l'accusé contre le schéma et le confronte à la commande, comme une
transcription ; aucun modèle ne le relit, et le contrôle de l'expéditeur, propre au mail,
ne s'applique pas."""
from __future__ import annotations
import importlib
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Reponse:
    """Une réponse du laboratoire, telle que le canal l'a lue. Brute : rien d'interprété."""
    identifiant: str
    objet: str
    texte: str
    recu_le: datetime
    reference: str | None = None     # si le canal a su la retrouver (objet, en-tête)
    reference_explicite: bool = False        # True si elle vient d'un en-tête posé par l'agent, pas déduite du texte
    pieces: list[tuple[str, bytes]] = field(default_factory=list)
    de: str | None = None                    # l'adresse de l'expéditeur, en minuscules
    # ce que la boîte sait de l'expéditeur (DKIM, SPF, DMARC) : True si rien n'échoue,
    # False si la boîte dit qu'il échoue, None si elle ne dit rien
    authentifie: bool | None = None
    # l'identifiant du message auquel celui-ci répond (In-Reply-To), la clé la plus sûre pour
    # retrouver la commande envoyée ; et le fil entier (References), moins précis
    en_reponse_a: list[str] = field(default_factory=list)
    fil: list[str] = field(default_factory=list)
    # L'accusé déjà structuré, rendu par un canal ERP : l'agent le prend tel quel, sans modèle
    accuse: dict | None = None


class BackendBase:
    # L'origine de l'accusé : « transcription » quand le canal lit une réponse humaine (un mail,
    # un PDF) qu'un agent transcrit ; « erp » seulement quand la réponse vient d'un système du
    # fournisseur. Un backend ERP met origine = "erp". Le mail ne peut pas dire autre chose.
    origine = "transcription"

    def envoyer(self, destinataire: str, copie: str, objet: str, texte: str, html: str,
                pieces: list[tuple[str, bytes, str]] | None = None, reference: str | None = None,
                nom_affiche: str | None = None, message_id: str | None = None) -> str:
        """Transmet au fournisseur. Rend un identifiant d'envoi (id de message, numéro ERP).
        nom_affiche : le nom de l'officine, montré comme expéditeur quand le canal le permet.
        message_id : l'identifiant que l'agent donne à cet envoi, pour reconnaître la réponse
        qui le cite (In-Reply-To) ; un canal mail le met en Message-ID."""
        raise NotImplementedError

    def lire_reponses(self, depuis: datetime | None = None) -> list[Reponse]:
        """Ce que le fournisseur a répondu et que l'agent n'a pas encore acquitté. Vide si rien.
        Lire ne doit rien marquer : une réponse est rendue à chaque passe jusqu'à `acquitter`."""
        raise NotImplementedError

    def acquitter(self, identifiants: list[str]) -> None:
        """Ces réponses sont traitées, ne plus les rendre. Appelé après la relecture."""
        raise NotImplementedError


def charger(config):
    if config.backend == "mail":
        from .mail import Backend
    else:
        Backend = importlib.import_module(f"pharma2a.backends.{config.backend}").Backend
    return Backend(config)
