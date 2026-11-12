"""La relecture des réponses, sans réseau ni modèle : une réponse .eml déposée dans
sorties/reponses/ avec son accusé préparé (.accuse.json), et la tâche change d'état."""
import json
import pathlib
import uuid
import pytest
from starlette.testclient import TestClient
from google.protobuf.json_format import MessageToDict
from a2a.helpers import new_data_message
from a2a.types import SendMessageRequest, Role
from pharma2a.config import Config
from pharma2a.serveur import creer_app
from pharma2a.dictionnaire import MEDIA
from pharma2a.extraction import ExtracteurFichier, schema_de_sortie

EXEMPLES = pathlib.Path(__file__).resolve().parent.parent / "schema" / "exemples"
JETON = "jeton-de-test"
AUTH = {"Authorization": f"Bearer {JETON}", "A2A-Version": "1.0"}
EML = (b"From: adv@labo.example\r\nTo: agent@exemple\r\nSubject: RE: Commande BC-26018-EXEMPLE - Pharmacie de l'Exemple\r\n"
       b"Date: Thu, 15 Oct 2026 15:00:00 +0200\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
       b"Bonjour, commande enregistr\xc3\xa9e sous SO-884512, d\xc3\xa9part le 17/10.\r\n")


@pytest.fixture
def agent(tmp_path):
    sorties = tmp_path / "sorties"
    config = Config(jeton=JETON, fournisseur="LABO UN", brouillon=True, sorties=sorties, relecture_minutes=0,
                    destinataire="adv@labo.example")
    app = creer_app(config, extracteur=ExtracteurFichier(sorties / "reponses"))
    with TestClient(app) as c:
        c.app_ = app; c.sorties = sorties
        yield c


def envoyer_commande(c):
    doc = json.loads((EXEMPLES / "commande.json").read_text(encoding="utf-8"))
    m = new_data_message(doc, media_type=MEDIA["commande"], role=Role.ROLE_USER); m.message_id = str(uuid.uuid4())
    r = c.post("/message:send", json=MessageToDict(SendMessageRequest(message=m)), headers=AUTH)
    assert r.json()["task"]["status"]["state"] == "TASK_STATE_WORKING"
    return r.json()["task"]["id"]


def deposer_reponse(c, accuse):
    d = c.sorties / "reponses"; d.mkdir(parents=True, exist_ok=True)
    (d / "r1.eml").write_bytes(EML)
    (d / "r1.accuse.json").write_text(json.dumps(accuse, ensure_ascii=False), encoding="utf-8")


def relire(c):
    import asyncio
    return asyncio.get_event_loop().run_until_complete(c.app_.state.relire()) if False else c.portal.call(c.app_.state.relire)


def test_reponse_a_trancher_passe_la_tache_en_input_required(agent):
    tid = envoyer_commande(agent)
    deposer_reponse(agent, json.loads((EXEMPLES / "accuse_reception.json").read_text(encoding="utf-8")))
    assert relire(agent) == 1
    t = agent.get(f"/tasks/{tid}", headers=AUTH).json()
    assert t["status"]["state"] == "TASK_STATE_INPUT_REQUIRED", t["status"]
    art = t["artifacts"][0]["parts"][0]
    assert art["mediaType"] == MEDIA["accuse_reception"]
    assert art["data"]["numero_commande_labo"] == "SO-884512"
    assert "trancher" in t["status"]["message"]["parts"][0]["text"]
    # la réponse lue est rangée : une seconde passe ne trouve rien
    assert relire(agent) == 0
    assert (agent.sorties / "reponses" / "lues" / "r1.eml").exists()


def test_reponse_conforme_termine_la_tache(agent):
    tid = envoyer_commande(agent)
    deposer_reponse(agent, {"version": "1", "reference": "BC-26018-EXEMPLE", "numero_commande_labo": "SO-1",
                            "date_livraison_prevue": "2026-10-20",
                            "lignes": [{"cip13": "3400000000011", "quantite_confirmee": 6, "reliquat": 0, "rupture": False}],
                            "commentaire": "enregistrée"})
    assert relire(agent) == 1
    t = agent.get(f"/tasks/{tid}", headers=AUTH).json()
    assert t["status"]["state"] == "TASK_STATE_COMPLETED"
    assert "livraison prévue le 2026-10-20" in t["status"]["message"]["parts"][0]["text"]


def test_accuse_non_conforme_laisse_la_tache_en_cours(agent):
    tid = envoyer_commande(agent)
    deposer_reponse(agent, {"version": "1", "reference": "BC-26018-EXEMPLE", "prix_total": 120,
                            "lignes": [{"cip13": "3400000000011", "quantite_confirmee": 6}]})
    assert relire(agent) == 0
    assert agent.get(f"/tasks/{tid}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_WORKING"
    # la réponse est acquittée : relire ne la rend plus, elle ne coûte pas une extraction par passe
    assert (agent.sorties / "reponses" / "lues" / "r1.eml").exists()


def test_reponse_sans_commande_en_cours_est_ignoree(agent):
    d = agent.sorties / "reponses"; d.mkdir(parents=True)
    (d / "r1.eml").write_bytes(EML.replace(b"BC-26018-EXEMPLE", b"BC-99999-INCONNU"))
    assert relire(agent) == 0


def test_schema_de_sortie_pour_le_modele():
    s = schema_de_sortie()
    assert s["additionalProperties"] is False and set(s["required"]) == set(s["properties"])
    assert "pattern" not in json.dumps(s) and "$schema" not in s
    assert s["properties"]["version"]["enum"] == ["1"]


def test_une_quantite_changee_par_le_labo_est_a_trancher(agent):
    tid = envoyer_commande(agent)
    deposer_reponse(agent, {"version": "1", "reference": "BC-26018-EXEMPLE", "numero_commande_labo": "SO-2",
                            "lignes": [{"cip13": "3400000000028", "quantite_confirmee": 12, "reliquat": 0, "rupture": False,
                                        "commentaire": "colisage par 12"}]})
    assert relire(agent) == 1
    t = agent.get(f"/tasks/{tid}", headers=AUTH).json()
    assert t["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert "quantité changée" in t["status"]["message"]["parts"][0]["text"]


EML_AVEC_PIECE = (b"From: adv@labo.example\r\nTo: agent@exemple\r\nSubject: RE: Proposition de commande BC-26018-EXEMPLE\r\n"
                  b"Date: Thu, 15 Oct 2026 15:00:00 +0200\r\nMIME-Version: 1.0\r\n"
                  b"Content-Type: multipart/mixed; boundary=\"B\"\r\n\r\n"
                  b"--B\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
                  b"<p>Bonjour, franco 300 &euro; HT, livraison sous 5 jours, P7 par 6.</p><p>Catalogue joint.</p>\r\n"
                  b"--B\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename=\"catalogue.pdf\"\r\n"
                  b"Content-Transfer-Encoding: base64\r\n\r\nJVBERi0xLjQKJSBjYXRhbG9ndWU=\r\n--B--\r\n")


def test_reponse_a_une_proposition_rapporte_conditions_colisage_et_catalogue(agent):
    """Le commercial répond à une proposition (HTML seul, catalogue joint) : le franco, le délai
    et le colisage passent dans l'accusé, le catalogue remonte en pièce jointe de la tâche."""
    tid = envoyer_commande(agent)
    d = agent.sorties / "reponses"; d.mkdir(parents=True, exist_ok=True)
    (d / "r2.eml").write_bytes(EML_AVEC_PIECE)
    (d / "r2.accuse.json").write_text(json.dumps({
        "version": "1", "reference": "BC-26018-EXEMPLE",
        "lignes": [{"cip13": "3400000000011", "quantite_confirmee": 6, "colisage": 6}],
        "conditions": {"franco_ht": 300, "delai_livraison_jours": 5, "catalogue_joint": True}}), encoding="utf-8")
    assert relire(agent) == 1
    t = agent.get(f"/tasks/{tid}", headers=AUTH).json()
    accuse = next(a for a in t["artifacts"] if a["name"] == "accuse_reception")["parts"][0]["data"]
    assert accuse["conditions"]["franco_ht"] == 300 and accuse["lignes"][0]["colisage"] == 6
    from pharma2a.dictionnaire import EXTENSION
    assert next(a for a in t["artifacts"] if a["name"] == "accuse_reception")["extensions"] == [EXTENSION]
    pieces = next(a for a in t["artifacts"] if a["name"] == "pieces_jointes")["parts"]
    assert pieces[0]["filename"] == "catalogue.pdf" and pieces[0]["mediaType"] == "application/pdf"
    import base64
    assert base64.b64decode(pieces[0]["raw"]).startswith(b"%PDF")


def test_le_texte_d_un_mail_html_seul_est_lu():
    from pharma2a.backends.mail import Backend
    r = Backend._depuis_eml(EML_AVEC_PIECE, "x")
    assert "franco 300" in r.texte and "<p>" not in r.texte
    assert r.reference == "BC-26018-EXEMPLE" and r.pieces[0][0] == "catalogue.pdf"


def test_l_envoi_gmail_signe_son_jeton_et_part_de_la_boite(tmp_path, monkeypatch):
    """Le compte de service signe une assertion pour l'expéditeur, Google rend un jeton, le
    message brut part par l'API Gmail ; le Message-ID porte le domaine de l'expéditeur."""
    import base64
    import email
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    from pharma2a.backends import mail
    cle = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = cle.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    compte = tmp_path / "compte.json"
    compte.write_text(json.dumps({"client_email": "agent@projet.iam.gserviceaccount.com", "private_key": pem,
                                  "token_uri": "https://oauth2.googleapis.com/token"}))
    appels = []
    class Rep:
        def __init__(self, corps): self.corps = corps
        def read(self): return self.corps
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def faux(req, timeout=0):
        appels.append(req)
        return Rep(b'{"access_token": "jeton"}' if "oauth2" in req.full_url else b'{"id": "abc"}')
    monkeypatch.setattr(mail.urllib.request, "urlopen", faux)
    c = Config(jeton=JETON, brouillon=False, expediteur="commandes@sydera.example", gmail_compte=str(compte))
    assert mail.Backend(c).envoyer("delegue@labo.example", "", "Commande X", "texte", "<p>html</p>", reference="BC-1") == "gmail:abc"
    jeton, envoi = appels
    assert b"jwt-bearer" in jeton.data
    corps = json.loads(envoi.data)
    m = email.message_from_bytes(base64.urlsafe_b64decode(corps["raw"]))
    assert m["To"] == "delegue@labo.example" and m["Message-ID"].endswith("@sydera.example>")
    assert envoi.headers["Authorization"] == "Bearer jeton"


def test_l_envoi_gmail_par_l_autorisation_de_la_boite(tmp_path, monkeypatch):
    """Sans clé de compte de service : le jeton de renouvellement donné une fois par la boîte."""
    from pharma2a.backends import mail
    f = tmp_path / "oauth.json"
    f.write_text(json.dumps({"client_id": "id", "client_secret": "s", "refresh_token": "r", "token_uri": "https://oauth2.googleapis.com/token"}))
    appels = []
    class Rep:
        def __init__(self, corps): self.corps = corps
        def read(self): return self.corps
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def faux(req, timeout=0):
        appels.append(req)
        return Rep(b'{"access_token": "jeton"}' if "oauth2" in req.full_url else b'{"id": "xyz"}')
    monkeypatch.setattr(mail.urllib.request, "urlopen", faux)
    c = Config(jeton=JETON, brouillon=False, expediteur="commandes@sydera.example", gmail_compte=str(f))
    assert mail.Backend(c).envoyer("d@labo.example", "", "Commande", "t", None) == "gmail:xyz"
    assert b"grant_type=refresh_token" in appels[0].data


def test_un_transfert_de_l_officine_n_est_pas_une_reponse(tmp_path):
    """L'officine transfère la commande à son commercial avec la même référence : ce n'est pas
    le laboratoire qui répond, la tâche reste en cours."""
    sorties = tmp_path / "sorties"
    config = Config(jeton=JETON, fournisseur="LABO UN", brouillon=True, sorties=sorties, relecture_minutes=0,
                    destinataire="adv@labo.example", copie="achats@officine.example",
                    expediteur="commandes@sydera.example")
    app = creer_app(config, extracteur=ExtracteurFichier(sorties / "reponses"))
    with TestClient(app) as c:
        c.app_ = app; c.sorties = sorties
        tid = envoyer_commande(c)
        d = sorties / "reponses"; d.mkdir(parents=True)
        accuse = json.loads((EXEMPLES / "accuse_reception.json").read_text(encoding="utf-8"))
        for nom, de in (("r1", b"pharmacien@officine.example"), ("r2", b"commandes@sydera.example"),
                        ("r4", b"titulaire@officine.example")):
            (d / f"{nom}.eml").write_bytes(EML.replace(b"adv@labo.example", de))
            (d / f"{nom}.accuse.json").write_text(json.dumps(accuse, ensure_ascii=False), encoding="utf-8")
        assert relire(c) == 0
        assert c.get(f"/tasks/{tid}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_WORKING"
        (d / "r3.eml").write_bytes(EML)
        (d / "r3.accuse.json").write_text(json.dumps(accuse, ensure_ascii=False), encoding="utf-8")
        assert relire(c) == 1


def test_un_mail_qui_ne_dit_rien_ne_confirme_pas(agent):
    tid = envoyer_commande(agent)
    deposer_reponse(agent, {"version": "1", "reference": "BC-26018-EXEMPLE", "lignes": [],
                            "commentaire": "peux-tu regarder ?"})
    assert relire(agent) == 0
    # des conditions toutes nulles ne disent rien non plus
    deposer_reponse(agent, {"version": "1", "reference": "BC-26018-EXEMPLE", "lignes": [],
                            "conditions": {"franco_ht": None, "delai_livraison_jours": None, "catalogue_joint": False},
                            "commentaire": "le colisage et le franco seraient utiles"})
    assert relire(agent) == 0
    # une ligne qui ne porte qu'une question non plus
    deposer_reponse(agent, {"version": "1", "reference": "BC-26018-EXEMPLE",
                            "lignes": [{"cip13": "3400000000066", "quantite_confirmee": None, "reliquat": None, "rupture": None,
                                        "cip13_substitue": None, "colisage": None, "commentaire": "Pourquoi cette ligne ?"}]})
    assert relire(agent) == 0
    assert agent.get(f"/tasks/{tid}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_WORKING"


# L'origine de l'accusé : le canal mail transcrit, il ne peut pas dire « erp » ; et
# l'id_ligne d'une réponse humaine se retrouve par le code de la ligne commandée.

def test_le_canal_mail_ne_produit_jamais_un_accuse_erp(agent):
    tid = envoyer_commande(agent)
    deposer_reponse(agent, {"version": "1", "reference": "BC-26018-EXEMPLE", "origine": "erp",
                            "numero_commande_labo": "SO-1",
                            "lignes": [{"id_ligne": "1", "cip13": "3400000000011", "quantite_confirmee": 6, "reliquat": 0, "rupture": False}]})
    assert relire(agent) == 1
    t = agent.get(f"/tasks/{tid}", headers=AUTH).json()
    accuse = next(a for a in t["artifacts"] if a["name"] == "accuse_reception")["parts"][0]["data"]
    assert accuse["origine"] == "transcription"
    from pharma2a.backends.mail import Backend
    from pharma2a.backends import BackendBase
    assert Backend.origine == "transcription" and BackendBase.origine == "transcription"


def test_l_id_ligne_d_une_reponse_humaine_vient_de_la_commande(agent):
    tid = envoyer_commande(agent)
    deposer_reponse(agent, {"version": "1", "reference": "BC-26018-EXEMPLE", "numero_commande_labo": "SO-3",
                            "lignes": [{"cip13": "3400000000028", "quantite_confirmee": 12, "reliquat": 0, "rupture": False}]})
    assert relire(agent) == 1
    t = agent.get(f"/tasks/{tid}", headers=AUTH).json()
    accuse = next(a for a in t["artifacts"] if a["name"] == "accuse_reception")["parts"][0]["data"]
    assert accuse["lignes"][0]["id_ligne"] == "2" and accuse["origine"] == "transcription"
    assert t["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"      # 12 au lieu de 21 : à trancher


def test_le_schema_du_modele_ne_porte_ni_origine_ni_anyof():
    s = schema_de_sortie()
    assert "origine" not in s["properties"] and "anyOf" not in json.dumps(s)
    assert "id_ligne" in s["properties"]["lignes"]["items"]["properties"]


def test_le_modele_se_nomme_dans_la_configuration_jamais_dans_le_code():
    from pharma2a.extraction import ExtracteurAnthropic
    assert any("PHARMA2A_MODELE" in m for m in Config(jeton=JETON, modele_cle="x").verifier())
    assert not any("PHARMA2A_MODELE" in m for m in Config(jeton=JETON, modele_cle="x", modele="un-modele").verifier())
    with pytest.raises(RuntimeError):
        ExtracteurAnthropic(Config(jeton=JETON, modele_cle="x"))


# La relecture confronte l'accusé à la commande : la référence, les id_ligne. Et elle
# n'acquitte une réponse auprès du canal qu'une fois traitée ou écartée pour de bon.

def test_un_accuse_qui_ne_repond_pas_a_cette_commande_est_ecarte(agent):
    tid = envoyer_commande(agent)
    for accuse in ({"version": "1", "reference": "BC-99999-AUTRE", "numero_commande_labo": "SO-1",
                    "lignes": [{"id_ligne": "1", "cip13": "3400000000011", "quantite_confirmee": 6}]},
                   {"version": "1", "reference": "BC-26018-EXEMPLE", "numero_commande_labo": "SO-1",
                    "lignes": [{"id_ligne": "9", "cip13": "3400000000011", "quantite_confirmee": 6}]},
                   {"version": "1", "reference": "BC-26018-EXEMPLE", "numero_commande_labo": "SO-1",
                    "lignes": [{"id_ligne": "1", "cip13": "3400000000011", "quantite_confirmee": 6},
                               {"id_ligne": "1", "cip13": "3400000000028", "quantite_confirmee": 21}]}):
        deposer_reponse(agent, accuse)
        assert relire(agent) == 0
        assert (agent.sorties / "reponses" / "lues" / "r1.eml").exists()      # écartée pour de bon
        (agent.sorties / "reponses" / "lues" / "r1.eml").unlink()
    assert agent.get(f"/tasks/{tid}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_WORKING"


def test_deux_taches_en_cours_pour_une_reference_rendent_la_reponse_ambigue(agent):
    """La proposition et la commande ferme partagent la référence ; tant que les deux sont en
    cours, une réponse ne se rattache pas, elle attend dans la boîte."""
    doc = json.loads((EXEMPLES / "commande.json").read_text(encoding="utf-8"))
    doc["nature"] = "proposition"; doc["questions"] = ["colisage"]
    m = new_data_message(doc, media_type=MEDIA["commande"], role=Role.ROLE_USER); m.message_id = str(uuid.uuid4())
    tp = agent.post("/message:send", json=MessageToDict(SendMessageRequest(message=m)), headers=AUTH).json()["task"]["id"]
    tc = envoyer_commande(agent)
    deposer_reponse(agent, {"version": "1", "reference": "BC-26018-EXEMPLE", "numero_commande_labo": "SO-1",
                            "lignes": [{"id_ligne": "1", "cip13": "3400000000011", "quantite_confirmee": 6}]})
    assert relire(agent) == 0
    assert (agent.sorties / "reponses" / "r1.eml").exists()      # pas acquittée : elle attend
    agent.post(f"/tasks/{tp}:cancel", json={}, headers=AUTH)
    assert relire(agent) == 1
    assert agent.get(f"/tasks/{tc}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_COMPLETED"


def test_une_commande_ferme_repondue_sans_confirmation_est_a_trancher(agent):
    """Un colisage, une condition ou un catalogue ne confirment pas les quantités d'une commande
    ferme : le pharmacien tranche. Pour une proposition, c'est la réponse attendue."""
    tid = envoyer_commande(agent)
    deposer_reponse(agent, {"version": "1", "reference": "BC-26018-EXEMPLE",
                            "lignes": [{"id_ligne": "1", "cip13": "3400000000011", "colisage": 6}],
                            "conditions": {"franco_ht": 300, "delai_livraison_jours": None, "catalogue_joint": None}})
    assert relire(agent) == 1
    t = agent.get(f"/tasks/{tid}", headers=AUTH).json()
    assert t["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert "sans confirmation" in t["status"]["message"]["parts"][0]["text"]
    doc = json.loads((EXEMPLES / "commande.json").read_text(encoding="utf-8"))
    doc["reference"] = "BC-26019-EXEMPLE"; doc["nature"] = "proposition"; doc["questions"] = ["colisage", "franco"]
    m = new_data_message(doc, media_type=MEDIA["commande"], role=Role.ROLE_USER); m.message_id = str(uuid.uuid4())
    tp = agent.post("/message:send", json=MessageToDict(SendMessageRequest(message=m)), headers=AUTH).json()["task"]["id"]
    d = agent.sorties / "reponses"
    (d / "r2.eml").write_bytes(EML.replace(b"BC-26018-EXEMPLE", b"BC-26019-EXEMPLE"))
    (d / "r2.accuse.json").write_text(json.dumps({"version": "1", "reference": "BC-26019-EXEMPLE",
                                                  "lignes": [{"id_ligne": "1", "cip13": "3400000000011", "colisage": 6}],
                                                  "conditions": {"franco_ht": 300}}), encoding="utf-8")
    assert relire(agent) == 1
    t = agent.get(f"/tasks/{tp}", headers=AUTH).json()
    assert t["status"]["state"] == "TASK_STATE_COMPLETED" and "proposition répondue" in t["status"]["message"]["parts"][0]["text"]


def test_un_expediteur_que_la_boite_dit_usurpe_est_ecarte(agent):
    from pharma2a.backends.mail import authentifie
    assert authentifie([]) is None
    # sans serveur de confiance, jamais de verdict positif : un « pass » peut venir de n'importe qui
    assert authentifie(["mx.example; dkim=pass header.d=labo.example; spf=pass"]) is None
    assert authentifie(["mx.example; dkim=none; spf=none"]) is None        # rien ne passe, rien n'échoue : on ne sait pas
    assert authentifie(["mx.example; dkim=fail; spf=softfail"]) is False
    assert authentifie(["mx.example; dkim=fail; spf=pass"]) is None
    assert authentifie(["mx.example; dmarc=fail; dkim=pass; spf=pass"]) is False
    assert authentifie(["evil.example; dmarc=pass"], "adv@labo.example") is None
    assert authentifie(["mx.example; dkim=pass header.d=labo.example"], "adv@labo.example", "mx.example") is True
    # un en-tête sans identifiant de serveur ne fait pas tomber la passe
    assert authentifie(["; dmarc=pass"], "adv@labo.example", "mx.example") is None
    assert authentifie(["; dmarc=fail"], "adv@labo.example") is False
    tid = envoyer_commande(agent)
    accuse = {"version": "1", "reference": "BC-26018-EXEMPLE", "numero_commande_labo": "SO-1",
              "lignes": [{"id_ligne": "1", "cip13": "3400000000011", "quantite_confirmee": 6}]}
    d = agent.sorties / "reponses"; d.mkdir(parents=True, exist_ok=True)
    (d / "r1.eml").write_bytes(b"Authentication-Results: mx.example; dkim=fail header.d=labo.example; spf=fail\r\n" + EML)
    (d / "r1.accuse.json").write_text(json.dumps(accuse), encoding="utf-8")
    assert relire(agent) == 0
    assert agent.get(f"/tasks/{tid}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_WORKING"
    (d / "r2.eml").write_bytes(b"Authentication-Results: mx.example; dkim=pass header.d=labo.example; spf=pass\r\n" + EML)
    (d / "r2.accuse.json").write_text(json.dumps(accuse), encoding="utf-8")
    assert relire(agent) == 1


def test_sans_domaine_connu_aucune_reponse_n_est_lue(tmp_path):
    sorties = tmp_path / "sorties"
    config = Config(jeton=JETON, fournisseur="LABO UN", brouillon=True, sorties=sorties, relecture_minutes=0)
    app = creer_app(config, extracteur=ExtracteurFichier(sorties / "reponses"))
    with TestClient(app) as c:
        c.app_ = app; c.sorties = sorties
        tid = envoyer_commande(c)
        deposer_reponse(c, json.loads((EXEMPLES / "accuse_reception.json").read_text(encoding="utf-8")))
        assert relire(c) == 0
        assert c.get(f"/tasks/{tid}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_WORKING"


def test_une_panne_du_modele_laisse_la_reponse_dans_la_boite(tmp_path):
    """Le modèle injoignable : la réponse n'est pas acquittée, elle est relue à la passe suivante.
    Un refus (RuntimeError) est définitif : acquittée."""
    class Panne:
        def __init__(self): self.appels = 0
        def extraire(self, texte, commande):
            self.appels += 1
            raise ConnectionError("modèle injoignable")
    sorties = tmp_path / "sorties"
    config = Config(jeton=JETON, fournisseur="LABO UN", brouillon=True, sorties=sorties, relecture_minutes=0, destinataire="adv@labo.example")
    panne = Panne()
    app = creer_app(config, extracteur=panne)
    with TestClient(app) as c:
        c.app_ = app; c.sorties = sorties
        envoyer_commande(c)
        deposer_reponse(c, {})
        assert relire(c) == 0 and relire(c) == 0
        assert panne.appels == 2 and (sorties / "reponses" / "r1.eml").exists()
    class Refus:
        def extraire(self, texte, commande): raise RuntimeError("le modèle a refusé")
    app = creer_app(config, extracteur=Refus())
    with TestClient(app) as c:
        c.app_ = app; c.sorties = sorties
        assert relire(c) == 0
        assert (sorties / "reponses" / "lues" / "r1.eml").exists()


def test_la_reference_se_retrouve_dans_l_objet_quelle_que_soit_sa_forme(agent):
    """Un autre agent d'achat numérote comme il veut : la relecture cherche dans l'objet les
    références qu'elle attend, pas seulement celles qui commencent par BC."""
    doc = json.loads((EXEMPLES / "commande.json").read_text(encoding="utf-8"))
    doc["reference"] = "CMD 2026/0042"
    m = new_data_message(doc, media_type=MEDIA["commande"], role=Role.ROLE_USER); m.message_id = str(uuid.uuid4())
    r = agent.post("/message:send", json=MessageToDict(SendMessageRequest(message=m)), headers=AUTH)
    tid = r.json()["task"]["id"]
    d = agent.sorties / "reponses"; d.mkdir(parents=True, exist_ok=True)
    (d / "r1.eml").write_bytes(EML.replace(b"Commande BC-26018-EXEMPLE", b"commande cmd 2026/0042"))
    (d / "r1.accuse.json").write_text(json.dumps({"version": "1", "reference": "CMD 2026/0042", "numero_commande_labo": "SO-1",
                                                  "lignes": [{"id_ligne": "1", "cip13": "3400000000011", "quantite_confirmee": 6}]}), encoding="utf-8")
    assert relire(agent) == 1
    assert agent.get(f"/tasks/{tid}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_COMPLETED"


# Deuxième relecture : le Message-ID retrouve la tâche, l'expéditeur départage les
# candidates, le verdict d'authentification vient du serveur de confiance, les codes de chaque
# ligne sont confrontés, une ligne confirmée ne confirme pas les autres, et la tâche est relue
# avant d'écrire.

def message_id_envoye(c):
    import email
    eml = next(f for f in c.sorties.glob("*.eml") if "etat-de-rayon" not in f.name)
    return email.message_from_bytes(eml.read_bytes())["Message-ID"]


def test_une_reponse_se_rattache_par_le_message_id_cite(agent):
    tid = envoyer_commande(agent)
    mid = message_id_envoye(agent)
    d = agent.sorties / "reponses"; d.mkdir(parents=True, exist_ok=True)
    # ni en-tête, ni référence dans l'objet : seul l'In-Reply-To dit à quoi on répond
    (d / "r1.eml").write_bytes(f"In-Reply-To: {mid}\r\n".encode() + EML.replace(b"RE: Commande BC-26018-EXEMPLE - Pharmacie de l'Exemple", b"RE: votre commande"))
    (d / "r1.accuse.json").write_text(json.dumps({"version": "1", "numero_commande_labo": "SO-1",
                                                  "lignes": [{"id_ligne": "1", "cip13": "3400000000011", "quantite_confirmee": 6}]}), encoding="utf-8")
    assert relire(agent) == 1
    assert agent.get(f"/tasks/{tid}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_COMPLETED"


def test_deux_fournisseurs_sous_la_meme_reference_se_distinguent_par_l_expediteur(tmp_path):
    """Deux agents de l'annuaire, la même officine, la même référence chez deux fournisseurs :
    la réponse de l'un ne se rattache qu'à sa tâche, et celle de l'autre attend."""
    f = tmp_path / "annuaire.json"
    f.write_text(json.dumps({"agents": [
        {"id": "labo-un", "fournisseur": "LABO UN", "jeton": "jeton-a", "destinataire": "adv@labo-un.example"},
        {"id": "labo-deux", "fournisseur": "LABO DEUX", "jeton": "jeton-b", "destinataire": "commandes@labo-deux.example"}]}), encoding="utf-8")
    sorties = tmp_path / "sorties"
    config = Config(annuaire=str(f), brouillon=True, sorties=sorties, relecture_minutes=0)
    app = creer_app(config, extracteur=ExtracteurFichier(sorties / "reponses"))
    doc = json.loads((EXEMPLES / "commande.json").read_text(encoding="utf-8"))
    with TestClient(app) as c:
        c.app_ = app; c.sorties = sorties
        ids = {}
        for chemin, jeton in (("labo-un", "jeton-a"), ("labo-deux", "jeton-b")):
            m = new_data_message(doc, media_type=MEDIA["commande"], role=Role.ROLE_USER); m.message_id = str(uuid.uuid4())
            r = c.post(f"/{chemin}/message:send", json=MessageToDict(SendMessageRequest(message=m)), headers={"Authorization": f"Bearer {jeton}", "A2A-Version": "1.0"})
            ids[chemin] = r.json()["task"]["id"]
        d = sorties / "reponses"; d.mkdir(parents=True, exist_ok=True)
        (d / "r1.eml").write_bytes(EML.replace(b"adv@labo.example", b"adv@labo-un.example"))
        (d / "r1.accuse.json").write_text(json.dumps({"version": "1", "reference": "BC-26018-EXEMPLE", "numero_commande_labo": "SO-1",
                                                      "lignes": [{"id_ligne": "1", "cip13": "3400000000011", "quantite_confirmee": 6}]}), encoding="utf-8")
        assert relire(c) == 1
        assert c.get(f"/labo-un/tasks/{ids['labo-un']}", headers={"Authorization": "Bearer jeton-a", "A2A-Version": "1.0"}).json()["status"]["state"] == "TASK_STATE_COMPLETED"
        assert c.get(f"/labo-deux/tasks/{ids['labo-deux']}", headers={"Authorization": "Bearer jeton-b", "A2A-Version": "1.0"}).json()["status"]["state"] == "TASK_STATE_WORKING"


def test_le_verdict_d_authentification_vient_du_serveur_de_confiance():
    from pharma2a.backends.mail import authentifie
    reel = "mx.google.com; dkim=fail header.d=labo.example; spf=fail smtp.mailfrom=labo.example; dmarc=fail"
    injecte = "mx.google.com; dmarc=pass; dkim=pass header.d=labo.example; spf=pass smtp.mailfrom=labo.example"
    # sans serveur de confiance : un échec quelque part suffit, le « pass » injecté ne sauve rien
    assert authentifie([injecte, reel], "adv@labo.example") is False
    assert authentifie([reel, injecte], "adv@labo.example") is False
    # avec le serveur de confiance : seul son premier en-tête compte, celui qu'il a mis en tête
    assert authentifie([reel, injecte], "adv@labo.example", "mx.google.com") is False
    assert authentifie(["autre.example; dmarc=pass", reel], "adv@labo.example", "mx.google.com") is False
    assert authentifie(["mx.google.com; dkim=pass header.d=labo.example", reel], "adv@labo.example", "mx.google.com") is True
    # l'alignement : un DKIM qui passe pour un autre domaine ne dit rien du From
    assert authentifie(["mx.google.com; dkim=pass header.d=autre.example"], "adv@labo.example", "mx.google.com") is None
    assert authentifie(["mx.google.com; dkim=pass header.d=labo.example"], "adv@sous.labo.example", "mx.google.com") is True
    assert authentifie(["mx.google.com; spf=pass smtp.mailfrom=labo.example"], "adv@labo.example", "mx.google.com") is True
    assert authentifie(["mx.google.com; dmarc=pass header.from=labo.example; dkim=fail"], "adv@labo.example", "mx.google.com") is True
    assert authentifie(["autre.example; dkim=pass"], "adv@labo.example", "mx.google.com") is None


def test_exiger_un_verdict_ecarte_une_reponse_non_verifiee(tmp_path):
    sorties = tmp_path / "sorties"
    config = Config(jeton=JETON, fournisseur="LABO UN", brouillon=True, sorties=sorties, relecture_minutes=0,
                    destinataire="adv@labo.example", exiger_authentification=True, imap_authserv="mx.example")
    app = creer_app(config, extracteur=ExtracteurFichier(sorties / "reponses"))
    accuse = {"version": "1", "reference": "BC-26018-EXEMPLE", "numero_commande_labo": "SO-1",
              "lignes": [{"id_ligne": "1", "cip13": "3400000000011", "quantite_confirmee": 6}]}
    with TestClient(app) as c:
        c.app_ = app; c.sorties = sorties
        tid = envoyer_commande(c)
        deposer_reponse(c, accuse)                      # aucun en-tête : pas de verdict
        assert relire(c) == 0
        d = sorties / "reponses"
        (d / "r2.eml").write_bytes(b"Authentication-Results: mx.example; dkim=pass header.d=labo.example\r\n" + EML)
        (d / "r2.accuse.json").write_text(json.dumps(accuse), encoding="utf-8")
        assert relire(c) == 1
        assert c.get(f"/tasks/{tid}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_COMPLETED"


def test_un_code_qui_n_est_pas_celui_de_la_ligne_designee_est_ecarte(agent):
    tid = envoyer_commande(agent)
    deposer_reponse(agent, {"version": "1", "reference": "BC-26018-EXEMPLE", "numero_commande_labo": "SO-1",
                            "lignes": [{"id_ligne": "1", "cip13": "3400000000028", "quantite_confirmee": 6}]})
    assert relire(agent) == 0
    assert agent.get(f"/tasks/{tid}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_WORKING"


def test_une_ligne_confirmee_ne_confirme_pas_les_autres(agent):
    """Sans numéro ni date, les lignes dont le fournisseur ne parle pas restent inconnues : à
    trancher. Avec un numéro de commande, la commande est enregistrée : confirmée."""
    tid = envoyer_commande(agent)
    deposer_reponse(agent, {"version": "1", "reference": "BC-26018-EXEMPLE",
                            "lignes": [{"id_ligne": "1", "cip13": "3400000000011", "quantite_confirmee": 6, "reliquat": 0, "rupture": False}]})
    assert relire(agent) == 1
    t = agent.get(f"/tasks/{tid}", headers=AUTH).json()
    assert t["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert "réponse partielle, 2 ligne(s) sans réponse" in t["status"]["message"]["parts"][0]["text"]
    doc = json.loads((EXEMPLES / "commande.json").read_text(encoding="utf-8")); doc["reference"] = "BC-26019-EXEMPLE"
    m = new_data_message(doc, media_type=MEDIA["commande"], role=Role.ROLE_USER); m.message_id = str(uuid.uuid4())
    t2 = agent.post("/message:send", json=MessageToDict(SendMessageRequest(message=m)), headers=AUTH).json()["task"]["id"]
    d = agent.sorties / "reponses"
    (d / "r2.eml").write_bytes(EML.replace(b"BC-26018-EXEMPLE", b"BC-26019-EXEMPLE"))
    (d / "r2.accuse.json").write_text(json.dumps({"version": "1", "reference": "BC-26019-EXEMPLE", "numero_commande_labo": "SO-2",
                                                  "lignes": [{"id_ligne": "1", "cip13": "3400000000011", "quantite_confirmee": 6}]}), encoding="utf-8")
    assert relire(agent) == 1
    assert agent.get(f"/tasks/{t2}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_COMPLETED"


def test_deux_reponses_dans_la_meme_passe_la_seconde_s_ajoute(agent):
    tid = envoyer_commande(agent)
    d = agent.sorties / "reponses"; d.mkdir(parents=True, exist_ok=True)
    for nom, accuse in (("r1", {"version": "1", "reference": "BC-26018-EXEMPLE", "numero_commande_labo": "SO-1",
                                "lignes": [{"id_ligne": "1", "cip13": "3400000000011", "quantite_confirmee": 6}]}),
                        ("r2", {"version": "1", "reference": "BC-26018-EXEMPLE",
                                "lignes": [{"id_ligne": "1", "cip13": "3400000000011", "quantite_confirmee": 0, "rupture": True}]})):
        (d / f"{nom}.eml").write_bytes(EML)
        (d / f"{nom}.accuse.json").write_text(json.dumps(accuse), encoding="utf-8")
    # Le second mail : « finalement en rupture » ne se perd plus, et le pharmacien
    # tranche ; la première tâche, terminée, ne se rouvre pas : une suite naît
    assert relire(agent) == 2
    t = agent.get(f"/tasks/{tid}", headers=AUTH).json()
    assert t["status"]["state"] == "TASK_STATE_COMPLETED"
    assert len([a for a in t["artifacts"] if a["name"] == "accuse_reception"]) == 1
    liste = agent.get("/tasks", params={"contextId": t["contextId"], "includeArtifacts": "true"}, headers=AUTH).json()["tasks"]
    [s] = [x for x in liste if (x.get("metadata") or {}).get("suite_de") == tid]
    assert s["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert "rupture" in s["status"]["message"]["parts"][0]["text"]
    assert (d / "lues" / "r1.eml").exists() and (d / "lues" / "r2.eml").exists()


def test_trois_reponses_dans_la_meme_passe_une_seule_suite(agent):
    tid = envoyer_commande(agent)
    d = agent.sorties / "reponses"; d.mkdir(parents=True, exist_ok=True)
    for nom, accuse in (("r1", {"version": "1", "reference": "BC-26018-EXEMPLE", "numero_commande_labo": "SO-1",
                                "lignes": [{"id_ligne": "1", "cip13": "3400000000011", "quantite_confirmee": 6}]}),
                        ("r2", {"version": "1", "reference": "BC-26018-EXEMPLE", "date_livraison_prevue": "2026-11-20", "lignes": []}),
                        ("r3", {"version": "1", "reference": "BC-26018-EXEMPLE", "date_livraison_prevue": "2026-11-27", "lignes": []})):
        (d / f"{nom}.eml").write_bytes(EML)
        (d / f"{nom}.accuse.json").write_text(json.dumps(accuse), encoding="utf-8")
    assert relire(agent) == 3
    t = agent.get(f"/tasks/{tid}", headers=AUTH).json()
    liste = agent.get("/tasks", params={"contextId": t["contextId"], "includeArtifacts": "true"}, headers=AUTH).json()["tasks"]
    suites = [x for x in liste if (x.get("metadata") or {}).get("suite_de")]
    # r2 complète la première (une suite terminée), r3 décale la livraison : une suite de la suite
    assert [x["metadata"]["suite_de"] for x in suites if x["metadata"]["suite_de"] == tid] == [tid]
    assert len(suites) == 2 and all(len([a for a in x["artifacts"] if a["name"] == "accuse_reception"]) == 1 for x in suites)


def test_la_reference_doit_apparaitre_entiere(agent):
    doc = json.loads((EXEMPLES / "commande.json").read_text(encoding="utf-8")); doc["reference"] = "CMD-42"
    m = new_data_message(doc, media_type=MEDIA["commande"], role=Role.ROLE_USER); m.message_id = str(uuid.uuid4())
    tid = agent.post("/message:send", json=MessageToDict(SendMessageRequest(message=m)), headers=AUTH).json()["task"]["id"]
    accuse = {"version": "1", "reference": "CMD-42", "numero_commande_labo": "SO-1",
              "lignes": [{"id_ligne": "1", "cip13": "3400000000011", "quantite_confirmee": 6}]}
    d = agent.sorties / "reponses"; d.mkdir(parents=True, exist_ok=True)
    (d / "r1.eml").write_bytes(EML.replace(b"RE: Commande BC-26018-EXEMPLE - Pharmacie de l'Exemple", b"RE: commande CMD-42-2"))
    (d / "r1.accuse.json").write_text(json.dumps(accuse), encoding="utf-8")
    assert relire(agent) == 0                      # CMD-42-2 n'est pas CMD-42
    (d / "r2.eml").write_bytes(EML.replace(b"RE: Commande BC-26018-EXEMPLE - Pharmacie de l'Exemple", b"RE: votre commande CMD-42."))
    (d / "r2.accuse.json").write_text(json.dumps(accuse), encoding="utf-8")
    assert relire(agent) == 1
    assert agent.get(f"/tasks/{tid}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_COMPLETED"


# Troisième relecture : In-Reply-To et References citent le même mail sans rendre la
# réponse ambiguë ; une réponse à un envoi fermé ne se rattache pas au suivant ; un code que la
# ligne commandée ne portait pas est refusé ; le contexte du modèle nomme chaque identifiant.

def test_in_reply_to_et_references_citant_le_meme_mail_ne_rendent_pas_la_reponse_ambigue(agent):
    tid = envoyer_commande(agent)
    mid = message_id_envoye(agent)
    d = agent.sorties / "reponses"; d.mkdir(parents=True, exist_ok=True)
    (d / "r1.eml").write_bytes(f"In-Reply-To: {mid}\r\nReferences: <ancien@labo.example> {mid}\r\n".encode()
                               + EML.replace(b"RE: Commande BC-26018-EXEMPLE - Pharmacie de l'Exemple", b"RE: votre commande"))
    (d / "r1.accuse.json").write_text(json.dumps({"version": "1", "numero_commande_labo": "SO-1",
                                                  "lignes": [{"id_ligne": "1", "cip13": "3400000000011", "quantite_confirmee": 6}]}), encoding="utf-8")
    assert relire(agent) == 1
    assert agent.get(f"/tasks/{tid}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_COMPLETED"


def test_une_reponse_a_un_envoi_ferme_ne_se_rattache_pas_au_suivant(agent):
    """L'officine ferme la tâche et renvoie la même référence ; la réponse du fournisseur au
    premier mail répond à ce premier mail : elle est écartée, et la seconde tâche attend."""
    t1 = envoyer_commande(agent)
    mid1 = message_id_envoye(agent)
    agent.post(f"/tasks/{t1}:cancel", json={}, headers=AUTH)
    t2 = envoyer_commande(agent)
    assert t2 != t1
    accuse = {"version": "1", "reference": "BC-26018-EXEMPLE", "numero_commande_labo": "SO-1",
              "lignes": [{"id_ligne": "1", "cip13": "3400000000011", "quantite_confirmee": 6}]}
    d = agent.sorties / "reponses"; d.mkdir(parents=True, exist_ok=True)
    (d / "r1.eml").write_bytes(f"In-Reply-To: {mid1}\r\n".encode() + EML)
    (d / "r1.accuse.json").write_text(json.dumps(accuse), encoding="utf-8")
    assert relire(agent) == 0
    assert (d / "lues" / "r1.eml").exists()
    assert agent.get(f"/tasks/{t2}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_WORKING"
    # la réponse au second mail, qui cite le premier dans son fil, se rattache au second
    import email
    emls = sorted(agent.sorties.glob("*.eml"), key=lambda f: f.stat().st_mtime_ns)
    mid2 = email.message_from_bytes(emls[-1].read_bytes())["Message-ID"]
    (d / "r2.eml").write_bytes(f"In-Reply-To: {mid2}\r\nReferences: {mid1} {mid2}\r\n".encode() + EML)
    (d / "r2.accuse.json").write_text(json.dumps(accuse), encoding="utf-8")
    assert relire(agent) == 1
    assert agent.get(f"/tasks/{t2}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_COMPLETED"


def test_un_code_que_la_ligne_commandee_ne_portait_pas_est_refuse(agent):
    doc = json.loads((EXEMPLES / "commande.json").read_text(encoding="utf-8"))
    doc["lignes"][0] = {"id_ligne": "1", "ref_fournisseur": "ACC-0042", "libelle": "PRESENTOIR DE COMPTOIR", "quantite": 1}
    m = new_data_message(doc, media_type=MEDIA["commande"], role=Role.ROLE_USER); m.message_id = str(uuid.uuid4())
    tid = agent.post("/message:send", json=MessageToDict(SendMessageRequest(message=m)), headers=AUTH).json()["task"]["id"]
    deposer_reponse(agent, {"version": "1", "reference": "BC-26018-EXEMPLE", "numero_commande_labo": "SO-1",
                            "lignes": [{"id_ligne": "1", "cip13": "3400000000099", "ref_fournisseur": "ACC-0042", "quantite_confirmee": 1}]})
    assert relire(agent) == 0
    deposer_reponse(agent, {"version": "1", "reference": "BC-26018-EXEMPLE", "numero_commande_labo": "SO-1",
                            "lignes": [{"id_ligne": "1", "ref_fournisseur": "ACC-0042", "quantite_confirmee": 1}]})
    (agent.sorties / "reponses" / "lues" / "r1.eml").unlink()
    assert relire(agent) == 1
    assert agent.get(f"/tasks/{tid}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_COMPLETED"


def test_le_contexte_du_modele_nomme_chaque_identifiant():
    from pharma2a.extraction import _contexte
    doc = json.loads((EXEMPLES / "commande.json").read_text(encoding="utf-8"))
    doc["lignes"][1] = {"id_ligne": "2", "ean13": "3600000000017", "ref_fournisseur": "BRO-01", "libelle": "BROSSE", "quantite": 12}
    c = _contexte(doc)
    assert "id_ligne 1, cip13 3400000000011 :" in c
    assert "id_ligne 2, ean13 3600000000017, ref_fournisseur BRO-01 :" in c
    assert "chacun dans son champ" in c and ", code " not in c


# Quatrième relecture : un mail illisible n'arrête pas la passe ; exiger un verdict
# suppose un serveur de confiance ; l'envoi d'une tâche sortie de « en cours » est clos.

def test_un_mail_illisible_est_ignore_et_acquitte_sans_arreter_la_passe(agent):
    tid = envoyer_commande(agent)
    d = agent.sorties / "reponses"; d.mkdir(parents=True, exist_ok=True)
    (d / "r0.eml").write_bytes(b"\xff\xfe\x00 pas un mail")
    from pharma2a.backends import mail
    vrai = mail.Backend._depuis_eml
    def casse(brut, identifiant, authserv=""):
        if identifiant == "r0.eml":
            raise ValueError("en-tête impossible")
        return vrai(brut, identifiant, authserv)
    mail.Backend._depuis_eml = staticmethod(casse)
    try:
        deposer_reponse(agent, {"version": "1", "reference": "BC-26018-EXEMPLE", "numero_commande_labo": "SO-1",
                                "lignes": [{"id_ligne": "1", "cip13": "3400000000011", "quantite_confirmee": 6}]})
        assert relire(agent) == 1
    finally:
        mail.Backend._depuis_eml = staticmethod(vrai)
    assert (d / "lues" / "r0.eml").exists() and (d / "lues" / "r1.eml").exists()
    assert agent.get(f"/tasks/{tid}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_COMPLETED"


def test_exiger_un_verdict_suppose_un_serveur_de_confiance():
    assert any("IMAP_AUTHSERV" in m for m in Config(jeton=JETON, exiger_authentification=True).verifier())
    assert not any("IMAP_AUTHSERV" in m for m in Config(jeton=JETON, exiger_authentification=True, imap_authserv="mx.example").verifier())


def test_une_seconde_reponse_au_meme_mail_dans_la_meme_passe_ne_retombe_pas_sur_la_reference(agent):
    """La première réponse termine la tâche ; la seconde, qui cite le même envoi, répond à un
    envoi clos : elle ne va pas chercher une autre tâche sous la même référence."""
    t1 = envoyer_commande(agent)
    mid1 = message_id_envoye(agent)
    doc = json.loads((EXEMPLES / "commande.json").read_text(encoding="utf-8")); doc["nature"] = "proposition"; doc["questions"] = ["colisage"]
    m = new_data_message(doc, media_type=MEDIA["commande"], role=Role.ROLE_USER); m.message_id = str(uuid.uuid4())
    tp = agent.post("/message:send", json=MessageToDict(SendMessageRequest(message=m)), headers=AUTH).json()["task"]["id"]
    d = agent.sorties / "reponses"; d.mkdir(parents=True, exist_ok=True)
    for nom, accuse in (("r1", {"version": "1", "reference": "BC-26018-EXEMPLE", "numero_commande_labo": "SO-1",
                                "lignes": [{"id_ligne": "1", "cip13": "3400000000011", "quantite_confirmee": 6}]}),
                        ("r2", {"version": "1", "reference": "BC-26018-EXEMPLE",
                                "lignes": [{"id_ligne": "1", "cip13": "3400000000011", "colisage": 6}]})):
        (d / f"{nom}.eml").write_bytes(f"In-Reply-To: {mid1}\r\n".encode() + EML)
        (d / f"{nom}.accuse.json").write_text(json.dumps(accuse), encoding="utf-8")
    assert relire(agent) == 1
    assert agent.get(f"/tasks/{t1}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_COMPLETED"
    assert agent.get(f"/tasks/{tp}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_WORKING"
    assert (d / "lues" / "r2.eml").exists()


# Cinquième relecture : un reliquat à zéro ne répond pas ; le fournisseur peut confirmer
# ou refuser globalement, en toutes lettres ; le parent le plus récent du fil décide.

def test_un_reliquat_a_zero_seul_ne_confirme_rien(agent):
    tid = envoyer_commande(agent)
    deposer_reponse(agent, {"version": "1", "reference": "BC-26018-EXEMPLE",
                            "lignes": [{"id_ligne": str(i), "cip13": c, "quantite_confirmee": None, "reliquat": 0, "rupture": None}
                                       for i, c in ((1, "3400000000011"), (2, "3400000000028"), (3, "3400000000035"))]})
    assert relire(agent) == 0
    assert agent.get(f"/tasks/{tid}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_WORKING"


def test_une_confirmation_globale_en_toutes_lettres_termine_la_tache(agent):
    tid = envoyer_commande(agent)
    deposer_reponse(agent, {"version": "1", "reference": "BC-26018-EXEMPLE", "lignes": [],
                            "confirmation_globale": True, "commentaire": "Commande confirmée intégralement telle que commandée."})
    assert relire(agent) == 1
    t = agent.get(f"/tasks/{tid}", headers=AUTH).json()
    assert t["status"]["state"] == "TASK_STATE_COMPLETED" and "confirmée telle que commandée" in t["status"]["message"]["parts"][0]["text"]
    doc = json.loads((EXEMPLES / "commande.json").read_text(encoding="utf-8")); doc["reference"] = "BC-26019-EXEMPLE"
    m = new_data_message(doc, media_type=MEDIA["commande"], role=Role.ROLE_USER); m.message_id = str(uuid.uuid4())
    t2 = agent.post("/message:send", json=MessageToDict(SendMessageRequest(message=m)), headers=AUTH).json()["task"]["id"]
    d = agent.sorties / "reponses"
    (d / "r2.eml").write_bytes(EML.replace(b"BC-26018-EXEMPLE", b"BC-26019-EXEMPLE"))
    (d / "r2.accuse.json").write_text(json.dumps({"version": "1", "reference": "BC-26019-EXEMPLE", "lignes": [],
                                                  "confirmation_globale": False, "commentaire": "Nous ne pouvons pas prendre cette commande."}), encoding="utf-8")
    assert relire(agent) == 1
    t = agent.get(f"/tasks/{t2}", headers=AUTH).json()
    assert t["status"]["state"] == "TASK_STATE_INPUT_REQUIRED" and "ne pas prendre" in t["status"]["message"]["parts"][0]["text"]


def test_le_parent_le_plus_recent_du_fil_decide(agent):
    """Sans In-Reply-To, le fil References va du plus ancien au plus récent : le dernier envoi
    connu est le parent. S'il est clos, la réponse est à lui, pas à un envoi plus ancien encore ouvert."""
    import email
    t1 = envoyer_commande(agent)                                     # reste ouvert
    mid1 = message_id_envoye(agent)
    doc = json.loads((EXEMPLES / "commande.json").read_text(encoding="utf-8")); doc["nature"] = "proposition"; doc["questions"] = ["colisage"]
    m = new_data_message(doc, media_type=MEDIA["commande"], role=Role.ROLE_USER); m.message_id = str(uuid.uuid4())
    tp = agent.post("/message:send", json=MessageToDict(SendMessageRequest(message=m)), headers=AUTH).json()["task"]["id"]
    emls = sorted(agent.sorties.glob("*.eml"), key=lambda f: f.stat().st_mtime_ns)
    mid2 = email.message_from_bytes(emls[-1].read_bytes())["Message-ID"]
    agent.post(f"/tasks/{tp}:cancel", json={}, headers=AUTH)       # le plus récent est clos
    accuse = {"version": "1", "reference": "BC-26018-EXEMPLE", "numero_commande_labo": "SO-1",
              "lignes": [{"id_ligne": "1", "cip13": "3400000000011", "quantite_confirmee": 6}]}
    d = agent.sorties / "reponses"; d.mkdir(parents=True, exist_ok=True)
    (d / "r1.eml").write_bytes(f"References: {mid1} {mid2}\r\n".encode() + EML.replace(b"RE: Commande BC-26018-EXEMPLE - Pharmacie de l'Exemple", b"RE: votre commande"))
    (d / "r1.accuse.json").write_text(json.dumps(accuse), encoding="utf-8")
    assert relire(agent) == 0                                        # réponse à l'envoi clos : écartée
    assert agent.get(f"/tasks/{t1}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_WORKING"
    (d / "r2.eml").write_bytes(f"References: {mid2} {mid1}\r\n".encode() + EML.replace(b"RE: Commande BC-26018-EXEMPLE - Pharmacie de l'Exemple", b"RE: votre commande"))
    (d / "r2.accuse.json").write_text(json.dumps(accuse), encoding="utf-8")
    assert relire(agent) == 1                                        # le parent est l'envoi ouvert
    assert agent.get(f"/tasks/{t1}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_COMPLETED"


# Sixième relecture : la référence déduite par le canal ne vaut pas l'en-tête, plusieurs
# destinataires dans une entrée, et l'identité de repli d'une officine.

def test_la_reference_deduite_par_le_canal_ne_prime_pas_sur_le_texte(agent):
    """« BC-42 2 » est une référence valide ; le raccourci du canal y lit « BC-42 ». Si BC-42 est
    aussi attendue, la réponse va à « BC-42 2 », la référence entière, pas au raccourci."""
    ids = {}
    for ref in ("BC-42", "BC-42 2"):
        doc = json.loads((EXEMPLES / "commande.json").read_text(encoding="utf-8")); doc["reference"] = ref
        m = new_data_message(doc, media_type=MEDIA["commande"], role=Role.ROLE_USER); m.message_id = str(uuid.uuid4())
        ids[ref] = agent.post("/message:send", json=MessageToDict(SendMessageRequest(message=m)), headers=AUTH).json()["task"]["id"]
    d = agent.sorties / "reponses"; d.mkdir(parents=True, exist_ok=True)
    (d / "r1.eml").write_bytes(EML.replace(b"RE: Commande BC-26018-EXEMPLE - Pharmacie de l'Exemple", b"RE: commande BC-42 2 - Pharmacie"))
    (d / "r1.accuse.json").write_text(json.dumps({"version": "1", "reference": "BC-42 2", "numero_commande_labo": "SO-1",
                                                  "lignes": [{"id_ligne": "1", "cip13": "3400000000011", "quantite_confirmee": 6}]}), encoding="utf-8")
    assert relire(agent) == 1
    assert agent.get(f"/tasks/{ids['BC-42 2']}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_COMPLETED"
    assert agent.get(f"/tasks/{ids['BC-42']}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_WORKING"


def test_plusieurs_destinataires_dans_une_entree_sont_tous_reconnus(tmp_path):
    sorties = tmp_path / "sorties"
    config = Config(jeton=JETON, fournisseur="LABO UN", brouillon=True, sorties=sorties, relecture_minutes=0,
                    destinataire="commandes@labo-un.example, adv@labo.example")
    app = creer_app(config, extracteur=ExtracteurFichier(sorties / "reponses"))
    with TestClient(app) as c:
        c.app_ = app; c.sorties = sorties
        tid = envoyer_commande(c)
        deposer_reponse(c, {"version": "1", "reference": "BC-26018-EXEMPLE", "numero_commande_labo": "SO-1",
                            "lignes": [{"id_ligne": "1", "cip13": "3400000000011", "quantite_confirmee": 6}]})
        assert relire(c) == 1
        assert c.get(f"/tasks/{tid}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_COMPLETED"


# Septième relecture : les copies de l'envoi sont de l'officine, jamais du fournisseur ;
# deux références citées à part restent ambiguës.

def test_une_reponse_depuis_la_copie_de_l_agent_n_est_pas_celle_du_fournisseur(tmp_path):
    f = tmp_path / "annuaire.json"
    f.write_text(json.dumps({"agents": [{"id": "labo-un", "fournisseur": "LABO UN", "jeton": "jeton-a",
                                         "destinataire": "adv@labo-un.example", "copie": "achats@officine.example, titulaire@officine.example"}]}), encoding="utf-8")
    sorties = tmp_path / "sorties"
    config = Config(annuaire=str(f), brouillon=True, sorties=sorties, relecture_minutes=0)
    app = creer_app(config, extracteur=ExtracteurFichier(sorties / "reponses"))
    doc = json.loads((EXEMPLES / "commande.json").read_text(encoding="utf-8"))
    accuse = {"version": "1", "reference": "BC-26018-EXEMPLE", "numero_commande_labo": "SO-1",
              "lignes": [{"id_ligne": "1", "cip13": "3400000000011", "quantite_confirmee": 6}]}
    with TestClient(app) as c:
        c.app_ = app; c.sorties = sorties
        m = new_data_message(doc, media_type=MEDIA["commande"], role=Role.ROLE_USER); m.message_id = str(uuid.uuid4())
        h = {"Authorization": "Bearer jeton-a", "A2A-Version": "1.0"}
        tid = c.post("/labo-un/message:send", json=MessageToDict(SendMessageRequest(message=m)), headers=h).json()["task"]["id"]
        d = sorties / "reponses"; d.mkdir(parents=True, exist_ok=True)
        (d / "r1.eml").write_bytes(EML.replace(b"adv@labo.example", b"titulaire@officine.example"))
        (d / "r1.accuse.json").write_text(json.dumps(accuse), encoding="utf-8")
        assert relire(c) == 0                                           # un transfert de l'officine
        assert c.get(f"/labo-un/tasks/{tid}", headers=h).json()["status"]["state"] == "TASK_STATE_WORKING"
        (d / "r2.eml").write_bytes(EML.replace(b"adv@labo.example", b"adv@labo-un.example"))
        (d / "r2.accuse.json").write_text(json.dumps(accuse), encoding="utf-8")
        assert relire(c) == 1


def test_deux_references_citees_a_part_restent_ambigues(agent):
    ids = {}
    for ref in ("BC-42", "BC-42 2"):
        doc = json.loads((EXEMPLES / "commande.json").read_text(encoding="utf-8")); doc["reference"] = ref
        m = new_data_message(doc, media_type=MEDIA["commande"], role=Role.ROLE_USER); m.message_id = str(uuid.uuid4())
        ids[ref] = agent.post("/message:send", json=MessageToDict(SendMessageRequest(message=m)), headers=AUTH).json()["task"]["id"]
    accuse = {"version": "1", "reference": "BC-42", "numero_commande_labo": "SO-1",
              "lignes": [{"id_ligne": "1", "cip13": "3400000000011", "quantite_confirmee": 6}]}
    d = agent.sorties / "reponses"; d.mkdir(parents=True, exist_ok=True)
    (d / "r1.eml").write_bytes(EML.replace(b"RE: Commande BC-26018-EXEMPLE - Pharmacie de l'Exemple", b"RE: vos commandes BC-42 et BC-42 2"))
    (d / "r1.accuse.json").write_text(json.dumps(accuse), encoding="utf-8")
    assert relire(agent) == 0
    assert (d / "r1.eml").exists()                                      # ambiguë : elle attend, pas acquittée
    (d / "r1.eml").write_bytes(EML.replace(b"RE: Commande BC-26018-EXEMPLE - Pharmacie de l'Exemple", b"RE: votre commande BC-42 - Pharmacie"))
    assert relire(agent) == 1
    assert agent.get(f"/tasks/{ids['BC-42']}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_COMPLETED"


def test_une_copie_sur_le_domaine_du_fournisseur_est_un_contact_du_fournisseur(tmp_path):
    """Le délégué en copie, sur le domaine du labo, répond : c'est le fournisseur. L'officine en
    copie, sur son propre domaine, répond : ce n'est pas lui."""
    sorties = tmp_path / "sorties"
    config = Config(jeton=JETON, fournisseur="LABO UN", brouillon=True, sorties=sorties, relecture_minutes=0,
                    destinataire="adv@labo.example", copie="delegue@labo.example, achats@officine.example")
    app = creer_app(config, extracteur=ExtracteurFichier(sorties / "reponses"))
    accuse = {"version": "1", "reference": "BC-26018-EXEMPLE", "numero_commande_labo": "SO-1",
              "lignes": [{"id_ligne": "1", "cip13": "3400000000011", "quantite_confirmee": 6}]}
    with TestClient(app) as c:
        c.app_ = app; c.sorties = sorties
        tid = envoyer_commande(c)
        d = sorties / "reponses"; d.mkdir(parents=True, exist_ok=True)
        (d / "r1.eml").write_bytes(EML.replace(b"adv@labo.example", b"achats@officine.example"))
        (d / "r1.accuse.json").write_text(json.dumps(accuse), encoding="utf-8")
        assert relire(c) == 0
        (d / "r2.eml").write_bytes(EML.replace(b"adv@labo.example", b"delegue@labo.example"))
        (d / "r2.accuse.json").write_text(json.dumps(accuse), encoding="utf-8")
        assert relire(c) == 1
        assert c.get(f"/tasks/{tid}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_COMPLETED"


def test_un_mail_joint_n_est_pas_lu_comme_la_reponse():
    """Un .eml transféré en pièce jointe garde son texte pour lui : il devient une pièce,
    sérialisée, et le corps de la réponse ne contient que ce que le fournisseur a écrit."""
    from pharma2a.backends.mail import Backend
    ancien = (b"From: adv@labo.example\r\nSubject: RE: Commande BC-26017-EXEMPLE\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
              b"Ancienne confirmation : commande enregistr\xc3\xa9e sous SO-0001, tout part.\r\n")
    brut = (b"From: adv@labo.example\r\nTo: agent@exemple\r\nSubject: RE: Commande BC-26018-EXEMPLE\r\nMIME-Version: 1.0\r\n"
            b"Content-Type: multipart/mixed; boundary=\"B\"\r\n\r\n"
            b"--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nVoir le message ci-joint, rien de nouveau.\r\n"
            b"--B\r\nContent-Type: message/rfc822\r\nContent-Disposition: attachment; filename=\"ancien.eml\"\r\n\r\n" + ancien + b"\r\n--B--\r\n")
    r = Backend._depuis_eml(brut, "x")
    assert "rien de nouveau" in r.texte and "SO-0001" not in r.texte and "Ancienne" not in r.texte
    assert len(r.pieces) == 1 and r.pieces[0][0] == "ancien.eml" and b"SO-0001" in r.pieces[0][1]


def test_un_conteneur_joint_n_est_pas_lu_comme_la_reponse():
    from pharma2a.backends.mail import Backend
    brut = (b"From: adv@labo.example\r\nTo: agent@exemple\r\nSubject: RE: Commande BC-26018-EXEMPLE\r\nMIME-Version: 1.0\r\n"
            b"Content-Type: multipart/mixed; boundary=\"B\"\r\n\r\n"
            b"--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nCi-joint l'ancien dossier.\r\n"
            b"--B\r\nContent-Type: multipart/alternative; boundary=\"C\"\r\nContent-Disposition: attachment; filename=\"dossier.mht\"\r\n\r\n"
            b"--C\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nAncienne confirmation SO-0001.\r\n--C--\r\n"
            b"--B--\r\n")
    r = Backend._depuis_eml(brut, "x")
    assert "ancien dossier" in r.texte and "SO-0001" not in r.texte
    assert len(r.pieces) == 1 and r.pieces[0][0] == "dossier.mht" and b"SO-0001" in r.pieces[0][1]


# « Attachment » avec une majuscule reste une pièce jointe, jamais le corps
def test_une_piece_jointe_en_majuscules_n_est_pas_le_corps():
    import email
    from pharma2a.backends.mail import _parties
    brut = (b"From: adv@labo.example\r\nSubject: RE: Commande\r\nMIME-Version: 1.0\r\n"
            b"Content-Type: multipart/mixed; boundary=X\r\n\r\n--X\r\nContent-Type: text/plain\r\n\r\nLe corps.\r\n"
            b"--X\r\nContent-Type: text/plain\r\nContent-Disposition: Attachment; filename=\"vieux.txt\"\r\n\r\nCONFIRME TOUT\r\n--X--\r\n")
    m = email.message_from_bytes(brut)
    corps = [p for p in _parties(m) if p.get_content_disposition() != "attachment"]
    assert len(corps) == 1 and "Le corps" in corps[0].get_payload()
    jointes = [p for p in _parties(m) if p.get_content_disposition() == "attachment"]
    assert len(jointes) == 1


# --- le rattachement filtre l'expéditeur avant ses priorités

def _tache(tid, etat, **meta):
    from a2a.types import Task, TaskStatus, TaskState
    t = Task(id=tid, context_id="c-" + tid, status=TaskStatus(state=getattr(TaskState, etat)))
    t.metadata.update({"reference": "BC-1", **meta})
    return t, {"reference": "BC-1", **meta}


def test_la_commande_en_cours_d_un_autre_fournisseur_n_ecarte_pas_la_sienne():
    from datetime import datetime, timezone
    from pharma2a.backends import Reponse
    from pharma2a.relecture import candidates_de
    a = _tache("a", "TASK_STATE_COMPLETED", destinataires=["adv@labo-a.example"], identites=["compte:1"])
    b = _tache("b", "TASK_STATE_WORKING", destinataires=["adv@labo-b.example"], identites=["compte:1"])
    r = Reponse(identifiant="m", objet="RE: BC-1", texte="", recu_le=datetime.now(timezone.utc), de="adv@labo-a.example")
    assert [t.id for t, _ in candidates_de(r, {}, set(), {"BC-1": [a, b]}, Config())] == ["a"]


def test_la_relance_d_une_officine_n_attire_pas_la_reponse_d_une_autre():
    from datetime import datetime, timezone
    from pharma2a.backends import Reponse
    from pharma2a.relecture import candidates_de
    dest = ["adv@labo.example"]
    rel = _tache("rel", "TASK_STATE_WORKING", destinataires=dest, identites=["compte:1"], nature="relance")
    autre = _tache("cmd2", "TASK_STATE_WORKING", destinataires=dest, identites=["compte:2"], nature="commande")
    r = Reponse(identifiant="m", objet="RE: BC-1", texte="", recu_le=datetime.now(timezone.utc), de="adv@labo.example")
    assert len(candidates_de(r, {}, set(), {"BC-1": [rel, autre]}, Config())) == 2      # ambigu : on attend
    # même officine, une commande et sa relance : sans le fil, on ne choisit pas non plus
    sienne = _tache("cmd1", "TASK_STATE_WORKING", destinataires=dest, identites=["compte:1"], nature="commande")
    assert len(candidates_de(r, {}, set(), {"BC-1": [rel, sienne]}, Config())) == 2
    # une seule commande possible : elle la reçoit
    assert [t.id for t, _ in candidates_de(r, {}, set(), {"BC-1": [sienne]}, Config())] == ["cmd1"]


def test_sans_transcription_la_reponse_attend_dans_la_boite(agent):
    """Revue du 4 novembre : sans modèle et sans accusé préparé, une vraie réponse n'est pas
    marquée lue ; elle attend qu'on puisse la transcrire."""
    envoyer_commande(agent)
    d = agent.sorties / "reponses"; d.mkdir(parents=True, exist_ok=True)
    (d / "r1.eml").write_bytes(EML)
    assert relire(agent) == 0
    assert (d / "r1.eml").exists() and not (d / "lues" / "r1.eml").exists()


def test_le_nom_des_pieces_jointes_ne_part_pas_au_modele(agent, monkeypatch):
    from pharma2a import extraction
    vu = {}
    def extraire(self, texte, commande, identifiant=None):
        vu["texte"] = texte
        raise extraction.PasDeTranscription("test")
    monkeypatch.setattr(extraction.ExtracteurFichier, "extraire", extraire)
    envoyer_commande(agent)
    d = agent.sorties / "reponses"; d.mkdir(parents=True, exist_ok=True)
    (d / "r1.eml").write_bytes(EML_AVEC_PIECE)
    relire(agent)
    assert "pièce(s) jointe(s)" in vu["texte"] and ".pdf" not in vu["texte"] and ".xlsx" not in vu["texte"]


def test_deux_dates_de_mail_avec_et_sans_fuseau_ne_bloquent_pas_la_relecture():
    """« -0000 » donne une date sans fuseau, « +0000 » une date avec : le tri les compare sans
    lever d'erreur, la plus ancienne d'abord."""
    from email.utils import parsedate_to_datetime
    from pharma2a.relecture import _date_comparable
    a = parsedate_to_datetime("Thu, 15 Oct 2026 15:00:00 -0000")
    b = parsedate_to_datetime("Thu, 15 Oct 2026 14:00:00 +0000")
    assert a.tzinfo is None and b.tzinfo is not None
    assert sorted([a, b, None], key=_date_comparable) == [None, b, a]


def test_un_mail_html_fait_de_balises_jamais_refermees_ne_fige_pas_la_passe():
    import time
    from pharma2a.backends.mail import _texte_du_html
    for motif in ("<script ", "<a ", "<style>x"):
        debut = time.monotonic()
        _texte_du_html(motif * (400_000 // len(motif)))
        assert time.monotonic() - debut < 2, motif          # ce mail demandait 45 secondes
    assert _texte_du_html("<p>Bonjour</p><script>a()</script><b>BC-1</b> 12 &lt; 13").split() == ["Bonjour", "BC-1", "12", "<", "13"]
    assert _texte_du_html("<style>p{}<p>jamais refermé</p>").split() == ["p{}", "jamais", "refermé"]


def test_un_jeu_de_caracteres_inconnu_ne_fait_pas_perdre_la_reponse():
    brut = ("From: commercial@labo.example\r\nSubject: Re: Commande BC-1\r\nMIME-Version: 1.0\r\n"
            "Content-Type: text/plain; charset=utf8mb4\r\nContent-Transfer-Encoding: 8bit\r\n\r\n"
            "Commande enregistrée, livraison jeudi.\r\n").encode("utf-8")
    from pharma2a.backends.mail import Backend
    r = Backend._depuis_eml(brut, "x")
    assert "Commande enregistrée, livraison jeudi." in r.texte
