"""Revue du 2 novembre : le courrier vérifie le certificat du serveur, en SMTP comme en IMAP,
avec un délai réseau ; un mail démesuré n'est pas téléchargé."""
import ssl
from pharma2a.backends import mail
from pharma2a.config import Config


class SMTPFaux:
    vu = {}

    def __init__(self, *a, **k):
        SMTPFaux.vu["timeout"] = k.get("timeout")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def starttls(self, context=None):
        SMTPFaux.vu["context"] = context

    def login(self, *a):
        pass

    def send_message(self, m):
        SMTPFaux.vu["envoye"] = True

    def quit(self):
        if SMTPFaux.vu.get("quit_421"):
            raise mail.smtplib.SMTPServerDisconnected("421 fermeture")

    def close(self):
        pass


def test_smtp_verifie_le_certificat(monkeypatch):
    monkeypatch.setattr(mail.smtplib, "SMTP", SMTPFaux)
    b = mail.Backend(Config(brouillon=False, smtp_hote="smtp.example", expediteur="agent@officine.example"))
    b.envoyer("adv@labo.example", "", "objet", "texte", "<p>texte</p>", reference="BC-1")
    ctx = SMTPFaux.vu["context"]
    assert isinstance(ctx, ssl.SSLContext) and ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert SMTPFaux.vu["timeout"]


class IMAPFaux:
    vu = {}

    def __init__(self, hote, port, ssl_context=None, timeout=None):
        IMAPFaux.vu.update(context=ssl_context, timeout=timeout)

    def login(self, *a):
        pass

    def select(self, *a):
        pass

    def response(self, code):
        if code == "PERMANENTFLAGS":
            return code, [IMAPFaux.vu.get("permanentflags", b"(\\Seen)")]
        return code, [IMAPFaux.vu.get("uidvalidity", b"42")]

    def logout(self):
        pass

    def uid(self, commande, *a):
        if commande == "search":
            return "OK", [b"7"]
        if a[1] == "(RFC822.SIZE)":
            return "OK", [b"7 (UID 7 RFC822.SIZE 99999999)"]
        raise AssertionError("un mail démesuré ne se télécharge pas")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def test_imap_verifie_le_certificat_et_ecarte_un_mail_demesure(monkeypatch, tmp_path):
    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", IMAPFaux)
    b = mail.Backend(Config(brouillon=False, imap_hote="imap.example", imap_utilisateur="u", imap_mot_de_passe="p", sorties=tmp_path))
    [r] = b.lire_reponses()
    ctx = IMAPFaux.vu["context"]
    assert isinstance(ctx, ssl.SSLContext) and ctx.verify_mode == ssl.CERT_REQUIRED and IMAPFaux.vu["timeout"]
    assert r.identifiant == "uid:INBOX:42:7" and r.texte == "" and r.reference is None


def test_un_quit_en_erreur_n_annule_pas_un_mail_accepte(monkeypatch):
    """Le serveur accepte le mail (DATA), puis répond 421 au QUIT : l'envoi a réussi, il ne
    doit pas être retenté, sinon la même commande partirait deux fois."""
    monkeypatch.setattr(mail.smtplib, "SMTP", SMTPFaux)
    SMTPFaux.vu.update(quit_421=True, envoye=False)
    b = mail.Backend(Config(brouillon=False, smtp_hote="smtp.example", expediteur="agent@officine.example"))
    mid = b.envoyer("adv@labo.example", "", "objet", "texte", "<p>texte</p>", reference="BC-1")
    assert SMTPFaux.vu["envoye"] and mid.startswith("<")
    SMTPFaux.vu["quit_421"] = False


def test_un_uid_d_une_boite_recreee_n_est_pas_acquitte(monkeypatch, tmp_path):
    """La boîte a été recréée (nouvelle UIDVALIDITY) : l'UID 7 lu avant désigne un autre message,
    il n'est pas marqué lu ; sous la même UIDVALIDITY, il l'est."""
    marques = []
    class IMAPMarque(IMAPFaux):
        def uid(self, commande, *a):
            if commande == "store":
                marques.append(a[0]); return "OK", []
            return super().uid(commande, *a)
    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", IMAPMarque)
    b = mail.Backend(Config(brouillon=False, imap_hote="imap.example", imap_utilisateur="u", imap_mot_de_passe="p", sorties=tmp_path))
    IMAPFaux.vu["uidvalidity"] = b"43"
    b.acquitter(["uid:INBOX:42:7"])
    assert marques == []
    IMAPFaux.vu["uidvalidity"] = b"42"
    b.acquitter(["uid:INBOX:42:7"])
    assert marques == ["7"]



def test_un_mail_ouvert_a_la_main_reste_a_traiter(monkeypatch, tmp_path):
    """La boîte accepte nos mots-clés : la recherche suit le mot-clé de l'agent (un humain qui
    ouvre la réponse ne la fait pas disparaître), et l'acquittement le pose."""
    vus = []
    class IMAPMots(IMAPFaux):
        def uid(self, commande, *a):
            vus.append((commande, a))
            if commande == "store":
                return "OK", []
            return super().uid(commande, *a)
    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", IMAPMots)
    IMAPFaux.vu["permanentflags"] = b"(\\Seen \\*)"
    b = mail.Backend(Config(brouillon=False, imap_hote="imap.example", imap_utilisateur="u", imap_mot_de_passe="p", sorties=tmp_path))
    b.lire_reponses()
    assert "UNKEYWORD PharmA2ALu" in vus[0][1][1]
    b.acquitter(["uid:INBOX:42:7"])
    assert vus[-1][0] == "store" and "PharmA2ALu" in vus[-1][1][2]
    IMAPFaux.vu["permanentflags"] = b"(\\Seen)"



def test_sans_mots_cles_un_mail_deja_lu_reste_a_traiter(monkeypatch, tmp_path):
    """La boîte refuse les mots-clés : la recherche prend tous les messages des trente jours (pas
    seulement les non lus), et l'agent note lui-même ce qu'il a traité."""
    vus = []
    class IMAPSansMots(IMAPFaux):
        def uid(self, commande, *a):
            vus.append((commande, a))
            if commande == "store":
                return "OK", []
            return super().uid(commande, *a)
    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", IMAPSansMots)
    b = mail.Backend(Config(brouillon=False, imap_hote="imap.example", imap_utilisateur="u", imap_mot_de_passe="p", sorties=tmp_path))
    [r] = b.lire_reponses()
    assert "UNSEEN" not in vus[0][1][1] and "SINCE" in vus[0][1][1]
    b.acquitter([r.identifiant])
    assert b.lire_reponses() == []          # notée traitée : elle ne revient plus
