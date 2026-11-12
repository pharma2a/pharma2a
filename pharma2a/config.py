"""Toute la configuration vient des variables d'environnement PHARMA2A_*.
Aucune adresse, aucun secret dans le code : ce fichier ne contient que des noms."""
from __future__ import annotations
import os
import pathlib
from dataclasses import dataclass, field


def _bool(valeur: str | None, defaut: bool) -> bool:
    if valeur is None or valeur.strip() == "":
        return defaut
    return valeur.strip().lower() in ("1", "true", "oui", "vrai", "yes")


@dataclass
class Config:
    # Les jetons des officines : le registre (JSON) des jetons que le fournisseur a délivrés,
    # chacun rattaché à un numéro de compte client (voir jetons.py). C'est la porte normale.
    jetons: str = ""
    # un jeton seul, pour un agent qui ne sert qu'une officine (ou pour les essais) ; rattaché
    # au compte client ci-dessous s'il est donné, sinon il vaut pour tout compte
    jeton: str = ""
    compte_client: str = ""
    # le fournisseur que cet agent représente, tel qu'il apparaît dans la carte
    fournisseur: str = "Fournisseur"
    # l'identifiant de l'agent dans l'annuaire (vide pour un agent seul) : ses tâches sont à lui
    agent: str = ""
    # le canal vers le laboratoire : mail (défaut) ou un module backends/<nom>.py
    backend: str = "mail"
    # qui reçoit les commandes chez le laboratoire, et qui est en copie
    destinataire: str = ""
    copie: str = ""
    # l'adresse d'expédition et de réponse de l'agent
    expediteur: str = ""
    # BROUILLON : rien ne part, tout s'écrit dans sorties/ (défaut)
    brouillon: bool = True
    sorties: pathlib.Path = field(default_factory=lambda: pathlib.Path("sorties"))
    # réseau : l'agent n'écoute que sur l'adresse locale sauf demande explicite
    hote: str = "127.0.0.1"
    port: int = 8765
    # l'adresse que la carte annonce (celle que l'officine appelle)
    url: str = "http://127.0.0.1:8765"
    # 1 : pas de carte étendue annoncée, la carte publique dit déjà tout (les compétences y sont toujours)
    tout_public: bool = False
    # le site du fournisseur, annoncé dans la carte (provider.url) ; vide : l'adresse de l'agent
    site: str = ""
    # magasin des tâches : chemin d'un fichier sqlite, vide = en mémoire (perdu au redémarrage)
    base: str = ""
    # courrier sortant / entrant
    smtp_hote: str = ""
    smtp_port: int = 587
    smtp_utilisateur: str = ""
    smtp_mot_de_passe: str = ""
    imap_hote: str = ""
    imap_port: int = 993
    imap_utilisateur: str = ""
    imap_mot_de_passe: str = ""
    imap_dossier: str = "INBOX"
    # le serveur de réception dont on croit les en-têtes Authentication-Results (DKIM, SPF,
    # DMARC), ex. mx.google.com pour une boîte Gmail ; vide : tout en-tête compte, et un seul
    # échec écarte la réponse
    imap_authserv: str = ""
    # exiger un verdict d'authentification : une réponse que la boîte n'a pas vérifiée est écartée
    exiger_authentification: bool = False
    # courrier sortant par l'API Brevo (HTTPS) plutôt que SMTP : la clé suffit
    brevo_cle: str = ""
    # le fichier JSON d'un compte de service Google à délégation sur le domaine : le mail part
    # de la boîte de l'expéditeur par l'API Gmail (prioritaire sur Brevo et le SMTP)
    gmail_compte: str = ""
    # relecture des réponses : toutes les N minutes (0 = jamais, on relit à la main)
    relecture_minutes: int = 10
    # le modèle qui transcrit la réponse du fournisseur (SDK Anthropic), nommé ici et jamais
    # dans le code ; sans clé ni modèle, l'accusé est lu dans un fichier préparé (brouillon, tests)
    modele: str = ""
    modele_cle: str = ""
    # plusieurs agents dans un processus : le fichier JSON de l'annuaire (vide = un seul agent)
    annuaire: str = ""
    # la fiche du fournisseur (JSON du dictionnaire) : servie sans clé sur /fiche_fournisseur.json
    fiche: str = ""
    # Le tarif de la fiche : sans clé, la vitrine le masque, sauf si le fournisseur le rend public ;
    # l'officine authentifiée le reçoit toujours (compétence fiche_fournisseur)
    fiche_tarif_public: bool = False

    @classmethod
    def depuis_env(cls, env: dict | None = None) -> "Config":
        e = os.environ if env is None else env
        # une variable laissée vide vaut une variable absente : le .env.example se charge tel quel
        g = lambda n, d="": e.get(f"PHARMA2A_{n}") or d
        return cls(
            jetons=g("JETONS"), jeton=g("JETON"), compte_client=g("COMPTE_CLIENT"),
            # PHARMA2A_LABORATOIRE : l'ancien nom de la variable, encore lu
            fournisseur=g("FOURNISSEUR") or g("LABORATOIRE") or "Fournisseur", backend=g("BACKEND", "mail"),
            destinataire=g("DESTINATAIRE"), copie=g("COPIE"), expediteur=g("EXPEDITEUR"),
            brouillon=_bool(e.get("PHARMA2A_BROUILLON"), True),
            sorties=pathlib.Path(g("SORTIES", "sorties")),
            hote=g("HOTE", "127.0.0.1"), port=int(g("PORT", "8765")),
            url=g("URL", f"http://{g('HOTE', '127.0.0.1')}:{g('PORT', '8765')}"),
            tout_public=_bool(e.get("PHARMA2A_TOUT_PUBLIC"), False), site=g("SITE"),
            base=g("BASE"),
            smtp_hote=g("SMTP_HOTE"), smtp_port=int(g("SMTP_PORT", "587")),
            # un mot de passe d'application Google se copie avec des espaces : on les retire
            smtp_utilisateur=g("SMTP_UTILISATEUR"), smtp_mot_de_passe=g("SMTP_MOT_DE_PASSE").replace(" ", ""),
            imap_hote=g("IMAP_HOTE"), imap_port=int(g("IMAP_PORT", "993")),
            imap_utilisateur=g("IMAP_UTILISATEUR"), imap_mot_de_passe=g("IMAP_MOT_DE_PASSE").replace(" ", ""),
            imap_dossier=g("IMAP_DOSSIER", "INBOX"), imap_authserv=g("IMAP_AUTHSERV"),
            exiger_authentification=_bool(e.get("PHARMA2A_EXIGER_AUTHENTIFICATION"), False),
            brevo_cle=g("BREVO_CLE"), gmail_compte=g("GMAIL_COMPTE"),
            relecture_minutes=int(g("RELECTURE_MINUTES", "10")),
            modele=g("MODELE"), modele_cle=g("MODELE_CLE"),
            annuaire=g("ANNUAIRE"), fiche=g("FICHE"), fiche_tarif_public=_bool(e.get("PHARMA2A_FICHE_TARIF_PUBLIC"), False),
        )

    def verifier(self) -> list[str]:
        """Ce qui manque pour démarrer. En brouillon, seule une porte est exigée."""
        manque = []
        if not self.jetons and not self.jeton and not self.annuaire:
            manque.append("PHARMA2A_JETONS (le registre des jetons des officines), PHARMA2A_JETON, ou PHARMA2A_ANNUAIRE")
        # un registre (plusieurs officines) ou un jeton unique (une officine), jamais les deux :
        # un jeton qui vaudrait pour tout compte à côté du registre mêlerait les officines
        if self.jetons and self.jeton:
            manque.append("PHARMA2A_JETONS ou PHARMA2A_JETON, pas les deux (le registre sert plusieurs officines, le jeton unique une seule)")
        if self.exiger_authentification and not self.imap_authserv:
            manque.append("PHARMA2A_IMAP_AUTHSERV (exiger l'authentification suppose un serveur de réception de confiance)")
        if self.modele_cle and not self.modele:
            manque.append("PHARMA2A_MODELE (le nom du modèle qui transcrit les réponses ; aucun défaut dans le code)")
        if not self.brouillon:
            if not self.destinataire and not self.annuaire:
                manque.append("PHARMA2A_DESTINATAIRE (qui reçoit les commandes au laboratoire)")
            if self.backend == "mail" and not self.expediteur:
                manque.append("PHARMA2A_EXPEDITEUR (hors brouillon, le mail doit partir)")
            if self.backend == "mail" and not (self.smtp_hote or self.brevo_cle or self.gmail_compte):
                manque.append("PHARMA2A_GMAIL_COMPTE, PHARMA2A_BREVO_CLE ou PHARMA2A_SMTP_HOTE (hors brouillon, un chemin d'envoi)")
        return manque
