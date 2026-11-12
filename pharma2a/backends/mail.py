"""Le canal mail : l'agent écrit au service commandes du laboratoire et lit ses réponses.
BROUILLON (défaut) : rien ne part, le message est écrit dans sorties/ tel qu'il serait
parti (.eml) avec son contenu validé (.json) ; les réponses se lisent dans
sorties/reponses/*.eml, ce qui permet de rejouer un échange complet sans réseau."""
from __future__ import annotations
import base64
import email
import email.policy
import email.utils
import html as html_mod
import json
import logging
import pathlib
import re
import shutil
import smtplib
import ssl
import imaplib
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from . import BackendBase, Reponse

REF = re.compile(r"\b(BC-[A-Z0-9][A-Z0-9._/-]*)\b")   # un raccourci (bon de commande) ; la relecture cherche aussi les références qu'elle attend


ECHECS = {"fail", "softfail", "permerror"}
MAX_MAIL = 30 * 1024 * 1024    # au-delà, le mail n'est pas téléchargé : il est écarté, et dit
DELAI_RESEAU = 60


def contexte_tls() -> ssl.SSLContext:
    """Le certificat du serveur se vérifie : sans contexte, smtplib et imaplib
    prennent un contexte qui ne vérifie rien, et un intermédiaire lirait le mot de passe."""
    return ssl.create_default_context()


def _parties(m):
    """Les parties du message, sans descendre dans les messages joints (message/rfc822) ni
    dans un conteneur marqué pièce jointe : leur contenu n'est pas le corps de la réponse."""
    if m.get_content_type() == "message/rfc822" or m.get_content_disposition() == "attachment":
        yield m
        return
    if m.is_multipart():
        for sous in m.get_payload():
            yield from _parties(sous)
    else:
        yield m


def _aligne(domaine_vu: str, domaine_from: str) -> bool:
    """Le domaine vérifié (header.d, smtp.mailfrom) est celui du From, ou un parent."""
    a, b = (domaine_vu or "").lower().strip("."), (domaine_from or "").lower().strip(".")
    return bool(a and b) and (a == b or b.endswith("." + a) or a.endswith("." + b))


def authentifie(entetes: list, de: str | None = None, authserv: str = "") -> bool | None:
    """Ce que la boîte de réception a vérifié de l'expéditeur, lu dans ses en-têtes
    Authentication-Results. Un From se forge, et un en-tête aussi : l'expéditeur peut en
    ajouter un qui dit « pass ». Donc :
    - avec un serveur de confiance (authserv, ex. mx.google.com), seul son premier en-tête
      compte, celui qu'il a mis en tête du message en le recevant ;
    - sans serveur de confiance, tous les en-têtes comptent, et un seul échec suffit à
      écarter : un « pass » ajouté ne sauve jamais un « fail » réel.
    Verdict : DMARC fait foi s'il est là (il vérifie l'alignement). Sinon un DKIM ou un SPF
    qui passe sur le domaine du From. False sur un échec, True sur un succès aligné, None
    quand la boîte ne dit rien d'utilisable."""
    entetes = [str(h) for h in (entetes or []) if str(h).strip()]
    if authserv:
        entetes = [h for h in entetes if _authserv_de(h) == authserv.lower()][:1]
    if not entetes:
        return None
    domaine_from = (de or "").rsplit("@", 1)[-1].lower() if de else ""
    # chaque en-tête rend son verdict ; le pire l'emporte : un « pass » ajouté par l'expéditeur
    # ne couvre jamais le « fail » du serveur qui a reçu le message
    verdicts = [_verdict(h, domaine_from) for h in entetes]
    if any(v is False for v in verdicts):
        return False
    # sans serveur de confiance, un « pass » peut venir de n'importe qui : il ne vaut rien
    if not authserv:
        return None
    return True if any(v is True for v in verdicts) else None


def _authserv_de(entete: str) -> str:
    """L'identifiant du serveur qui a écrit l'en-tête (avant le premier « ; »), ou vide."""
    premier = entete.split(";", 1)[0].strip().split()
    return premier[0].lower() if premier else ""


def _verdict(entete: str, domaine_from: str) -> bool | None:
    """Le verdict d'un en-tête Authentication-Results, à la manière de DMARC : DMARC s'il est
    là ; sinon un DKIM ou un SPF qui passe sur le domaine du From ; sinon un échec ; sinon rien."""
    resultats = re.findall(r"\b(dkim|spf|dmarc)\s*=\s*([a-z]+)((?:\s*(?:\([^)]*\))?\s*[a-z.]+=[^\s;]+)*)", entete, flags=re.I)
    if not resultats:
        return None
    dmarc = [v.lower() for m, v, _ in resultats if m.lower() == "dmarc"]
    if any(v in ECHECS for v in dmarc):
        return False
    if "pass" in dmarc:
        return True
    succes, echec = False, False
    for mecanisme, verdict, proprietes in resultats:
        mecanisme, verdict = mecanisme.lower(), verdict.lower()
        if mecanisme == "dmarc":
            continue
        if verdict in ECHECS:
            echec = True
            continue
        if verdict != "pass":
            continue
        props = dict(re.findall(r"([a-z.]+)=([^\s;]+)", proprietes, flags=re.I))
        if mecanisme == "dkim":
            domaine = props.get("header.d") or props.get("header.i", "").rsplit("@", 1)[-1]
        else:
            domaine = props.get("smtp.mailfrom", "").rsplit("@", 1)[-1] or props.get("smtp.helo", "")
        if not domaine_from or _aligne(domaine, domaine_from):
            succes = True
    if succes:
        return True
    return False if echec else None


_OUVRE = re.compile(r"<(script|style)", re.I | re.A)
_FERME = {b: re.compile(f"</{b}>", re.I | re.A) for b in ("script", "style")}


def _contenu(part) -> str:
    """Le texte d'une partie. Un jeu de caractères que Python ne connaît pas (utf8mb4, cp-850)
    ne fait pas perdre la réponse : elle se lit en UTF-8, les octets inconnus remplacés."""
    try:
        return part.get_content()
    except LookupError:
        return (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")


def _texte_du_html(h: str) -> str:
    """Le texte d'un mail HTML, balises retirées, en temps linéaire : un mail fait de balises
    jamais refermées, venu de n'importe qui, ne fige pas la passe de relecture."""
    morceaux, pos, suite, sans_fin = [], 0, 0, set()
    while (o := _OUVRE.search(h, suite)):
        bal = o.group(1).lower()
        f = None if bal in sans_fin else _FERME[bal].search(h, o.end())
        if not f:
            # jamais refermée : elle ne le sera pas plus loin, on ne cherche plus sa fermeture
            sans_fin.add(bal); suite = o.end(); continue
        morceaux.append(h[pos:o.start()]); pos = suite = f.end()
    h = " ".join(morceaux + [h[pos:]])
    h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", h)
    morceaux, pos = [], 0
    while (d := h.find("<", pos)) >= 0 and (f := h.find(">", d + 1)) >= 0:
        # de « < » au premier « > » : une balise, remplacée par une espace (« <> » reste tel quel)
        morceaux.append(h[pos:f + 1] if f == d + 1 else h[pos:d] + " "); pos = f + 1
    return html_mod.unescape("".join(morceaux) + h[pos:])


class Backend(BackendBase):
    origine = "transcription"      # un mail est lu par un agent, jamais écrit par un ERP

    def __init__(self, config):
        self.c = config

    def _message(self, destinataire, copie, objet, texte, html, pieces, reference, nom_affiche=None, message_id=None, en_reponse_a=None):
        m = EmailMessage()
        adresse = self.c.expediteur or "pharma2a@localhost"
        # le destinataire voit le nom de l'officine qui commande, l'adresse de l'agent dessous
        m["From"] = formataddr((nom_affiche, adresse)) if nom_affiche else adresse
        m["To"] = destinataire or self.c.destinataire
        if copie or self.c.copie:
            m["Cc"] = copie or self.c.copie
        m["Subject"] = objet
        m["Date"] = formatdate(localtime=True)
        # l'identifiant du message porte le domaine de l'expéditeur : « pharma2a.local » est un
        # signal de spam pour les filtres (Microsoft 365 en tête)
        m["Message-ID"] = message_id or make_msgid(domain=(adresse.rsplit("@", 1)[-1] if "@" in adresse else "localhost"))
        if reference:
            m["X-PharmA2A-Reference"] = reference
        if en_reponse_a:
            # La relance dans le fil de la commande : le fournisseur la lit sous son premier mail
            m["In-Reply-To"] = en_reponse_a
            m["References"] = en_reponse_a
        if self.c.expediteur:
            m["Reply-To"] = self.c.expediteur
        m.set_content(texte)
        if html:
            m.add_alternative(html, subtype="html")
        for nom, contenu, media in (pieces or []):
            maintype, _, subtype = (media or "application/octet-stream").partition("/")
            m.add_attachment(contenu, maintype=maintype, subtype=subtype or "octet-stream", filename=nom)
        return m

    def envoyer(self, destinataire, copie, objet, texte, html, pieces=None, reference=None, nom_affiche=None, message_id=None,
                en_reponse_a=None) -> str:
        m = self._message(destinataire, copie, objet, texte, html, pieces, reference, nom_affiche, message_id, en_reponse_a)
        if self.c.brouillon:
            d = pathlib.Path(self.c.sorties); d.mkdir(parents=True, exist_ok=True)
            horodatage = datetime.now().strftime("%Y%m%d-%H%M%S")
            nom = re.sub(r"[^A-Za-z0-9._-]+", "_", reference or objet)[:60]
            f = d / f"{horodatage}-{nom}.eml"
            n = 1
            while f.exists():      # deux envois dans la même seconde ne s'écrasent pas
                n += 1; f = d / f"{horodatage}-{nom}-{n}.eml"
            f.write_bytes(m.as_bytes())
            return f"brouillon:{f.name}"
        if self.c.gmail_compte:
            return self._envoyer_gmail(m)
        if self.c.brevo_cle:
            return self._envoyer_brevo(destinataire, copie, objet, texte, html, pieces, reference, nom_affiche, m["Message-ID"], en_reponse_a)
        s = smtplib.SMTP(self.c.smtp_hote, self.c.smtp_port, timeout=30)
        try:
            s.starttls(context=contexte_tls())
            if self.c.smtp_utilisateur:
                s.login(self.c.smtp_utilisateur, self.c.smtp_mot_de_passe)
            s.send_message(m)
        except BaseException:
            s.close()
            raise
        # LE MAIL EST PARTI DÈS QUE LE SERVEUR L'A ACCEPTÉ (après DATA) : une erreur à la
        # fermeture (421 au QUIT) n'est pas un échec d'envoi, sinon la même commande repartirait
        try:
            s.quit()
        except Exception as e:
            logging.getLogger("pharma2a.mail").warning("mail %s accepté par le serveur, fermeture SMTP en erreur (%s) : sans effet sur l'envoi", m["Message-ID"], e)
            s.close()
        return m["Message-ID"]

    def _jeton_gmail(self) -> str:
        """Un jeton d'accès Gmail pour l'expéditeur, valable une heure. Deux formes de fichier :
        l'autorisation donnée par la boîte elle-même (refresh_token), ou un compte de service à
        délégation sur le domaine, signé ici sans bibliothèque Google."""
        import time
        cle = json.loads(pathlib.Path(self.c.gmail_compte).expanduser().read_text())
        if cle.get("refresh_token"):
            # L'autorisation de la boîte : quand l'organisation interdit les clés de compte
            # de service, la boîte autorise l'agent une fois (gmail.send) et le jeton se renouvelle
            donnees = urllib.parse.urlencode({"client_id": cle["client_id"], "client_secret": cle["client_secret"],
                                              "refresh_token": cle["refresh_token"], "grant_type": "refresh_token"}).encode()
            req = urllib.request.Request(cle.get("token_uri", "https://oauth2.googleapis.com/token"), data=donnees,
                                         headers={"content-type": "application/x-www-form-urlencoded"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.loads(r.read())["access_token"]
            except urllib.error.HTTPError as e:
                raise RuntimeError(f"Google refuse le renouvellement {e.code} : {e.read().decode('utf-8', 'replace')[:200]}") from None
        # Le compte de service seul signe : cryptography n'est requise que pour lui (extra « gmail »)
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
        except ImportError:
            raise RuntimeError("le compte de service Gmail demande cryptography : pip install \"pharma2a[gmail]\"") from None
        b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=")
        maintenant = int(time.time())
        entete = b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        corps = b64(json.dumps({"iss": cle["client_email"], "sub": self.c.expediteur,
                                "scope": "https://www.googleapis.com/auth/gmail.send",
                                "aud": cle.get("token_uri", "https://oauth2.googleapis.com/token"),
                                "iat": maintenant, "exp": maintenant + 3600}).encode())
        privee = serialization.load_pem_private_key(cle["private_key"].encode(), password=None)
        signature = b64(privee.sign(entete + b"." + corps, padding.PKCS1v15(), hashes.SHA256()))
        assertion = (entete + b"." + corps + b"." + signature).decode()
        donnees = ("grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer&assertion=" + assertion).encode()
        req = urllib.request.Request(cle.get("token_uri", "https://oauth2.googleapis.com/token"), data=donnees,
                                     headers={"content-type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())["access_token"]
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Google refuse le jeton {e.code} : {e.read().decode('utf-8', 'replace')[:200]}") from None

    def _envoyer_gmail(self, m) -> str:
        """Le mail part de la boîte elle-même. L'API Gmail en
        HTTPS : les serveurs de Google, la signature du domaine, aucun lien de désabonnement ni
        pixel de suivi, et le message rangé dans les envoyés de la boîte qui lit les réponses."""
        jeton = self._jeton_gmail()
        brut = base64.urlsafe_b64encode(m.as_bytes()).decode()
        req = urllib.request.Request("https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                                     data=json.dumps({"raw": brut}).encode(),
                                     headers={"authorization": f"Bearer {jeton}", "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                rep = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Gmail {e.code} : {e.read().decode('utf-8', 'replace')[:200]}") from None
        return f"gmail:{rep.get('id', '?')}"

    def _envoyer_brevo(self, destinataire, copie, objet, texte, html, pieces, reference, nom_affiche, message_id, en_reponse_a=None) -> str:
        """L'API transactionnelle de Brevo, en HTTPS : pas de port SMTP à ouvrir, le domaine
        de l'expéditeur signe (SPF, DKIM), et la livraison se suit dans Brevo. Les réponses
        reviennent dans la boîte de l'expéditeur (Reply-To), que lire_reponses relit."""
        def adresses(s):
            return [{"email": x.strip()} for x in (s or "").split(",") if x.strip()]
        corps = {
            "sender": {"email": self.c.expediteur, **({"name": nom_affiche} if nom_affiche else {})},
            "to": adresses(destinataire or self.c.destinataire),
            "replyTo": {"email": self.c.expediteur},
            "subject": objet,
            "textContent": texte,
            "headers": {"X-PharmA2A-Reference": reference or "", "Message-ID": message_id,
                        **({"In-Reply-To": en_reponse_a, "References": en_reponse_a} if en_reponse_a else {})},
        }
        if html:
            corps["htmlContent"] = html
        cc = adresses(copie or self.c.copie)
        if cc:
            corps["cc"] = cc
        if pieces:
            corps["attachment"] = [{"name": nom, "content": base64.b64encode(contenu).decode()} for nom, contenu, _ in pieces]
        req = urllib.request.Request("https://api.brevo.com/v3/smtp/email", data=json.dumps(corps).encode("utf-8"),
                                     headers={"api-key": self.c.brevo_cle, "content-type": "application/json", "accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                rep = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Brevo {e.code} : {e.read().decode('utf-8', 'replace')[:200]}") from None
        return f"brevo:{rep.get('messageId', '?')}"

    def ecrire_contenu(self, reference: str, nom: str, doc: dict) -> None:
        """En brouillon seulement : le document validé à côté du .eml, pour relire ce qui est parti."""
        if not self.c.brouillon:
            return
        d = pathlib.Path(self.c.sorties); d.mkdir(parents=True, exist_ok=True)
        (d / f"{re.sub(r'[^A-Za-z0-9._-]+', '_', reference)}-{nom}.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")

    def lire_reponses(self, depuis: datetime | None = None) -> list[Reponse]:
        if self.c.brouillon:
            d = pathlib.Path(self.c.sorties) / "reponses"
            if not d.exists():
                return []
            return [self._lire(f.read_bytes(), f.name) for f in sorted(d.glob("*.eml"))]
        out = []
        with self._imap() as im:
            # seulement les messages NON LUS, lus SANS les marquer (BODY.PEEK) : un message
            # reste rendu à chaque passe tant que la relecture ne l'a pas acquitté, pour
            # qu'une panne passagère du modèle ne fasse pas perdre une réponse
            # UN MAIL OUVERT À LA MAIN RESTE À TRAITER (revue du 4 novembre) : le traitement se suit
            # par un mot-clé IMAP propre à l'agent, pas par le drapeau « lu » qu'un humain pose en
            # ouvrant la réponse ; sur les trente jours d'écoute. Un serveur qui refuse les
            # mots-clés retombe sur « non lu ».
            depuis_j = (depuis or (datetime.now(timezone.utc) - timedelta(days=30))).strftime("%d-%b-%Y")
            mots = _mots_cles(im)
            # sans mots-clés, tous les messages des trente jours, moins ceux que l'agent a notés
            # traités dans son propre registre : le drapeau « lu » d'un humain n'y change rien
            critere = f'(UNKEYWORD {MOT_CLE} SINCE "{depuis_j}")' if mots else f'(SINCE "{depuis_j}")'
            traites = set() if mots else set(self._traites())
            # L'IDENTITÉ D'UN MESSAGE EST SA BOÎTE, SON UIDVALIDITY ET SON UID (RFC 9051, 2.3.1.1) :
            # une boîte recréée réutilise les UID, et un nouvel accusé ne doit pas être pris pour
            # une réponse déjà traitée
            valide = _uidvalidity(im)
            pre = f"uid:{self.c.imap_dossier}:{valide}:"
            _, ids = im.uid("search", None, critere)
            for i in ids[0].split():
                if pre + i.decode() in traites:
                    continue
                # la taille d'abord : un mail démesuré ne se télécharge pas, il est écarté (et
                # acquitté comme une réponse vide), pour ne pas revenir à chaque passe
                _, info = im.uid("fetch", i, "(RFC822.SIZE)")
                m_taille = re.search(rb"RFC822\.SIZE (\d+)", info[0] if isinstance(info[0], bytes) else b"")
                if m_taille and int(m_taille.group(1)) > MAX_MAIL:
                    logging.getLogger("pharma2a.mail").warning("réponse uid:%s de %s octets : trop lourde, écartée", i.decode(), m_taille.group(1).decode())
                    out.append(Reponse(identifiant=pre + i.decode(), objet="", texte="", recu_le=datetime.now(timezone.utc)))
                    continue
                _, data = im.uid("fetch", i, "(BODY.PEEK[])")
                out.append(self._lire(data[0][1], pre + i.decode()))
        return out

    def _lire(self, brut: bytes, identifiant: str) -> Reponse:
        """Un mail qui ne se décode pas ne bloque pas la passe : il devient une réponse vide,
        que la relecture ignore et acquitte."""
        try:
            return self._depuis_eml(brut, identifiant, self.c.imap_authserv)
        except Exception as e:
            logging.getLogger("pharma2a.mail").warning("réponse %s illisible (%s) : ignorée", identifiant, e)
            return Reponse(identifiant=identifiant, objet="", texte="", recu_le=datetime.now(timezone.utc))

    def acquitter(self, identifiants: list[str]) -> None:
        """En brouillon, la réponse lue est rangée dans reponses/lues/ ; en IMAP, le message
        est marqué lu. Dans les deux cas il n'est plus rendu."""
        if self.c.brouillon:
            d = pathlib.Path(self.c.sorties) / "reponses"
            for ident in identifiants:
                src = d / ident
                if src.exists():
                    (d / "lues").mkdir(exist_ok=True)
                    shutil.move(str(src), str(d / "lues" / ident))
                    acc = d / (ident.rsplit(".", 1)[0] + ".accuse.json")
                    if acc.exists():
                        shutil.move(str(acc), str(d / "lues" / acc.name))
            return
        with self._imap() as im:
            # seulement les messages de cette boîte, sous la même UIDVALIDITY qu'à la lecture :
            # après une recréation de la boîte, un UID mémorisé désigne un autre message
            pre = f"uid:{self.c.imap_dossier}:{_uidvalidity(im)}:"
            uids = [i[len(pre):] for i in identifiants if i.startswith(pre)]
            if uids:
                mots = _mots_cles(im)
                im.uid("store", ",".join(uids), "+FLAGS", f"({MOT_CLE} \\Seen)" if mots else "(\\Seen)")
                if not mots:
                    self._noter_traites([pre + u for u in uids])

    def _registre_traites(self) -> pathlib.Path:
        return pathlib.Path(self.c.sorties) / ".imap-traites.json"

    def _traites(self) -> dict:
        """Les réponses que l'agent a traitées, quand la boîte refuse ses mots-clés : identifiant
        (boîte, UIDVALIDITY, UID) et date ; gardées quarante-cinq jours, au-delà de l'écoute."""
        try:
            return json.loads(self._registre_traites().read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _noter_traites(self, identifiants: list[str]) -> None:
        f = self._registre_traites()
        f.parent.mkdir(parents=True, exist_ok=True)
        limite = (datetime.now(timezone.utc) - timedelta(days=45)).date().isoformat()
        d = {k: v for k, v in self._traites().items() if v >= limite}
        d.update({i: datetime.now(timezone.utc).date().isoformat() for i in identifiants})
        tmp = f.with_name(f.name + ".tmp")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        tmp.replace(f)

    def _imap(self):
        im = imaplib.IMAP4_SSL(self.c.imap_hote, self.c.imap_port, ssl_context=contexte_tls(), timeout=DELAI_RESEAU)
        im.login(self.c.imap_utilisateur, self.c.imap_mot_de_passe)
        im.select(self.c.imap_dossier)
        return im

    @staticmethod
    def _depuis_eml(brut: bytes, identifiant: str, authserv: str = "") -> Reponse:
        m = email.message_from_bytes(brut, policy=email.policy.default)
        texte, pieces = "", []
        for part in _parties(m):
            # la disposition se lit sans casse (« Attachment » est une pièce jointe)
            jointe = part.get_content_disposition() == "attachment"
            if part.get_content_type() == "message/rfc822":
                # Un mail joint n'est pas la réponse : une ancienne confirmation transférée serait
                # transcrite comme la réponse du jour ; il reste une pièce, sérialisée, jamais lue
                joint = part.get_payload(0) if part.is_multipart() else None
                pieces.append((part.get_filename() or "message-joint.eml", joint.as_bytes() if joint is not None else (part.get_payload(decode=True) or b"")))
            elif part.get_content_type() == "text/plain" and not jointe:
                texte += _contenu(part)
            elif jointe:
                # un conteneur joint (multipart) est gardé entier, sérialisé
                contenu = part.as_bytes() if part.is_multipart() else (part.get_payload(decode=True) or b"")
                pieces.append((part.get_filename() or "piece", contenu))
        if not texte.strip():
            # un client mail qui n'envoie que du HTML : on en garde le texte, balises retirées
            for part in _parties(m):
                if part.get_content_type() == "text/html" and part.get_content_disposition() != "attachment":
                    texte += _texte_du_html(_contenu(part))
        objet = str(m.get("Subject") or "")
        ref = m.get("X-PharmA2A-Reference")
        explicite = bool(ref)
        if not ref:
            trouve = REF.search(objet) or REF.search(texte)
            ref = trouve.group(1) if trouve else None
        try:
            recu = email.utils.parsedate_to_datetime(m.get("Date")) if m.get("Date") else datetime.now(timezone.utc)
        except Exception:
            recu = datetime.now(timezone.utc)
        de = email.utils.parseaddr(str(m.get("From") or ""))[1].lower() or None
        cites = lambda h: [c.strip() for c in str(m.get(h) or "").split() if c.strip()]
        return Reponse(identifiant=identifiant, objet=objet, texte=texte, recu_le=recu, reference=ref, reference_explicite=explicite, pieces=pieces, de=de,
                       authentifie=authentifie(m.get_all("Authentication-Results") or [], de, authserv),
                       en_reponse_a=cites("In-Reply-To"), fil=cites("References"))


def _uidvalidity(im) -> str:
    """L'UIDVALIDITY de la boîte sélectionnée, telle que le serveur l'a annoncée au SELECT."""
    try:
        _, v = im.response("UIDVALIDITY")
        v = v[0] if v else None
        return v.decode() if isinstance(v, bytes) else str(v or "0")
    except Exception:
        return "0"


MOT_CLE = "PharmA2ALu"      # le mot-clé IMAP que l'agent pose sur une réponse traitée


def _mots_cles(im) -> bool:
    """La boîte accepte-t-elle des mots-clés à nous (\\* dans PERMANENTFLAGS, RFC 9051) ?"""
    try:
        _, v = im.response("PERMANENTFLAGS")
        return any(b"\\*" in (x if isinstance(x, bytes) else str(x).encode()) for x in (v or []) if x)
    except Exception:
        return False
