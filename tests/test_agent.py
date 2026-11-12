"""L'agent en mode brouillon, de bout en bout, sans réseau : carte, porte, commande,
état de rayon, rejet. Lancer, depuis pharma2a/ : python -m pytest"""
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

EXEMPLES = pathlib.Path(__file__).resolve().parent.parent / "schema" / "exemples"
JETON = "jeton-de-test"
AUTH = {"Authorization": f"Bearer {JETON}", "A2A-Version": "1.0"}


@pytest.fixture
def client(tmp_path):
    config = Config(jeton=JETON, fournisseur="LABO UN", brouillon=True, sorties=tmp_path / "sorties")
    with TestClient(creer_app(config)) as c:
        c.sorties = tmp_path / "sorties"
        yield c


def exemple(nom):
    return json.loads((EXEMPLES / f"{nom}.json").read_text(encoding="utf-8"))


def envoi(nom, doc):
    m = new_data_message(doc, media_type=MEDIA[nom], role=Role.ROLE_USER)
    m.message_id = str(uuid.uuid4())
    return MessageToDict(SendMessageRequest(message=m))


def test_carte_publique_minimale_sans_jeton(client):
    r = client.get("/.well-known/agent-card.json")
    assert r.status_code == 200
    carte = r.json()
    assert {s["id"] for s in carte["skills"]} == {"commander", "etat_de_rayon", "relancer", "fiche"}
    assert carte["provider"]["url"] == "http://127.0.0.1:8765" and carte["provider"]["organization"] == "LABO UN"
    assert carte["capabilities"].get("streaming", False) is False
    assert carte["capabilities"].get("pushNotifications", False) is False
    assert carte["capabilities"]["extendedAgentCard"] is True
    assert "bearer" in carte["securitySchemes"]
    assert {i["protocolBinding"] for i in carte["supportedInterfaces"]} == {"HTTP+JSON", "JSONRPC"}


def test_la_carte_declare_l_extension_pharma2a(client):
    """PharmA2A se déclare comme une extension d'A2A, facultative, avec le dictionnaire qu'elle porte."""
    from pharma2a.dictionnaire import EXTENSION
    ext = client.get("/.well-known/agent-card.json").json()["capabilities"]["extensions"]
    assert [e["uri"] for e in ext] == [EXTENSION] == ["https://pharma2a.org/ext/v1"]
    assert not ext[0].get("required")
    assert ext[0]["params"]["dictionnaire"] == "1"
    assert set(ext[0]["params"]["messages"]) == {"commande", "accuse_reception", "etat_de_rayon", "fiche_fournisseur", "relance"}


def test_l_extension_demandee_ou_non_le_service_est_le_meme(client):
    """Facultative : avec l'en-tête A2A-Extensions, sans lui, ou avec une extension inconnue,
    la commande est reçue pareil (le texte d'A2A : une extension inconnue s'ignore)."""
    from pharma2a.dictionnaire import EXTENSION
    for i, ext in enumerate((None, EXTENSION, "https://exemple.test/ext/inconnue/v1")):
        doc = exemple("commande"); doc["reference"] = f"BC-EXT-{i}"
        h = dict(AUTH, **({"A2A-Extensions": ext} if ext else {}))
        r = client.post("/message:send", json=envoi("commande", doc), headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["task"]["status"]["state"] == "TASK_STATE_WORKING", ext


def test_tout_le_reste_exige_le_jeton(client):
    assert client.post("/message:send", json=envoi("commande", exemple("commande"))).status_code == 401
    assert client.get("/extendedAgentCard").status_code == 401
    assert client.get("/tasks/x", headers={"Authorization": "Bearer mauvais"}).status_code == 401


def test_carte_etendue_porte_les_competences(client):
    r = client.get("/extendedAgentCard", headers=AUTH)
    assert r.status_code == 200
    assert {s["id"] for s in r.json()["skills"]} == {"commander", "etat_de_rayon", "relancer", "fiche"}


def test_commande_part_en_brouillon_et_reste_working(client):
    r = client.post("/message:send", json=envoi("commande", exemple("commande")), headers=AUTH)
    assert r.status_code == 200, r.text
    tache = r.json()["task"]
    assert tache["status"]["state"] == "TASK_STATE_WORKING", tache
    assert "BC-26018-EXEMPLE" in tache["status"]["message"]["parts"][0]["text"]
    emls = list(client.sorties.glob("*.eml"))
    assert len(emls) == 1
    brut = emls[0].read_bytes().decode("utf-8", "replace")
    assert "BC-26018-EXEMPLE" in brut and "3400000000011" in brut and "Pharmacie de l'Exemple" in brut
    assert "Compte client : 123456" in brut
    assert "€" not in brut and "prix" not in brut.lower()
    # la tâche se relit
    r2 = client.get(f"/tasks/{tache['id']}", headers=AUTH)
    assert r2.status_code == 200 and r2.json()["status"]["state"] == "TASK_STATE_WORKING"
    # et le contenu validé est à côté
    assert (client.sorties / "BC-26018-EXEMPLE-commande.json").exists()


def test_quantites_flottantes_du_transport_sont_remises_entieres(client):
    doc = exemple("commande"); doc["lignes"][0]["quantite"] = 6.0
    r = client.post("/message:send", json=envoi("commande", doc), headers=AUTH)
    assert r.json()["task"]["status"]["state"] == "TASK_STATE_WORKING"
    eml = next(client.sorties.glob("*.eml")).read_bytes().decode("utf-8", "replace")
    assert "6.0" not in eml


def test_un_prix_est_rejete(client):
    doc = exemple("commande"); doc["lignes"][0]["prix_ht"] = 12.5
    r = client.post("/message:send", json=envoi("commande", doc), headers=AUTH)
    assert r.status_code == 200
    assert r.json()["task"]["status"]["state"] == "TASK_STATE_REJECTED"
    assert "prix_ht" in r.json()["task"]["status"]["message"]["parts"][0]["text"]
    assert not list(client.sorties.glob("*.eml"))


def test_un_texte_brut_recoit_un_message_pas_une_tache(client):
    corps = {"message": {"messageId": str(uuid.uuid4()), "role": "ROLE_USER", "parts": [{"text": "{\"reference\": \"x\"}"}]}}
    r = client.post("/message:send", json=corps, headers=AUTH)
    assert "message" in r.json() and "task" not in r.json(), r.json()
    assert "PharmA2A" in r.json()["message"]["parts"][0]["text"]
    assert not list(client.sorties.glob("*.eml"))


def test_la_carte_a_ses_en_tetes_et_son_champ_skills(client):
    r = client.get("/.well-known/agent-card.json")
    assert len(r.json()["skills"]) == 4
    assert "max-age" in r.headers["cache-control"] and r.headers.get("last-modified") and r.headers.get("etag")
    assert client.get("/.well-known/agent-card.json", headers={"If-None-Match": r.headers["etag"]}).status_code == 304


def test_etat_de_rayon_part_en_piece_jointe_et_termine(client):
    r = client.post("/message:send", json=envoi("etat_de_rayon", exemple("etat_de_rayon")), headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
    eml = next(client.sorties.glob("*.eml")).read_bytes().decode("utf-8", "replace")
    assert "etat-de-rayon-labo-un-2026-10-15.xlsx" in eml and "spreadsheetml" in eml
    assert "CHEVEUX ET ONGLES" in eml  # le tableau est dans le mail, pas seulement en pièce jointe


def test_jsonrpc_meme_porte_meme_resultat(client):
    corps = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": envoi("commande", exemple("commande"))}
    r = client.post("/", json=corps, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["result"]["task"]["status"]["state"] == "TASK_STATE_WORKING", r.json()


def test_tout_public_n_annonce_plus_de_carte_etendue(tmp_path):
    config = Config(jeton=JETON, brouillon=True, sorties=tmp_path, tout_public=True, site="https://www.labo.example")
    with TestClient(creer_app(config)) as c:
        carte = c.get("/.well-known/agent-card.json").json()
        assert {s["id"] for s in carte["skills"]} == {"commander", "etat_de_rayon", "relancer", "fiche"}
        assert carte["capabilities"].get("extendedAgentCard", False) is False
        assert carte["provider"]["url"] == "https://www.labo.example"


def test_le_canal_relit_une_reponse_en_brouillon(tmp_path):
    from pharma2a.backends.mail import Backend
    config = Config(jeton=JETON, brouillon=True, sorties=tmp_path)
    b = Backend(config)
    (tmp_path / "reponses").mkdir()
    (tmp_path / "reponses" / "r1.eml").write_bytes(
        b"From: adv@labo.example\r\nTo: agent@exemple\r\nSubject: RE: Commande BC-26018-EXEMPLE - Pharmacie\r\n"
        b"Date: Thu, 15 Oct 2026 15:00:00 +0200\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Bonjour, commande enregistr\xc3\xa9e sous SO-1, livraison le 20/10.\r\n")
    reps = b.lire_reponses()
    assert len(reps) == 1 and reps[0].reference == "BC-26018-EXEMPLE"
    assert "SO-1" in reps[0].texte and reps[0].pieces == []


def test_la_commande_emporte_l_etat_de_rayon_en_un_seul_mail(client):
    doc = exemple("commande"); doc["etat_de_rayon"] = exemple("etat_de_rayon")
    r = client.post("/message:send", json=envoi("commande", doc), headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["task"]["status"]["state"] == "TASK_STATE_WORKING"
    emls = list(client.sorties.glob("*.eml")); assert len(emls) == 1
    eml = emls[0].read_bytes().decode("utf-8", "replace")
    assert "BC-26018-EXEMPLE" in eml and "commande-bc-26018-exemple-et-etat-de-rayon.xlsx" in eml
    doc["etat_de_rayon"]["lignes"][0]["prix"] = 3
    doc["reference"] = "BC-26019-EXEMPLE"      # une autre commande : la même référence rendrait la tâche déjà ouverte
    assert client.post("/message:send", json=envoi("commande", doc), headers=AUTH).json()["task"]["status"]["state"] == "TASK_STATE_REJECTED"


def test_une_proposition_pose_ses_questions_et_ne_s_expedie_pas():
    from pharma2a import courrier
    from pharma2a.dictionnaire import valider
    doc = json.loads((EXEMPLES / "commande.json").read_text(encoding="utf-8"))
    doc.update(nature="proposition", questions=["franco", "colisage", "catalogue"], agent={"nom": "Agent Exemple", "signataire": None})
    assert valider("commande", doc) == []
    objet, texte, html = courrier.commande(doc)
    assert objet.startswith("Proposition de commande") and "merci de confirmer" in objet
    assert "franco de port" in texte and "colisage" in texte and "tarif à jour" in texte
    assert "Rien à expédier" in texte and "Proposition de commande" in html
    doc["questions"] = ["remise"]
    assert valider("commande", doc) != []     # on ne demande jamais une remise par le dictionnaire


def test_le_bon_n_argumente_pas():
    """23/10 : le commercial n'est pas l'acheteur. Le bon donne la commande, le stock et les
    ventes, demande ce qui manque, et se tait : pas d'annotation « à zéro », pas de plaidoyer."""
    from pharma2a import courrier
    doc = json.loads((EXEMPLES / "commande.json").read_text(encoding="utf-8"))
    doc["etat_de_rayon"] = json.loads((EXEMPLES / "etat_de_rayon.json").read_text(encoding="utf-8"))
    doc["agent"] = {"nom": "Agent Exemple", "signataire": "Le titulaire"}
    _, texte, html = courrier.commande(doc)
    assert "à zéro" not in texte and "à zéro" not in html
    assert "Stock" in texte and "Ventes" in texte
    assert len(texte.splitlines()) <= 24
    assert "Sydera" not in html and "dans la minute" not in texte


def test_le_bon_montre_le_colisage_et_le_franco_quand_il_les_sait():
    """22/10, le laboratoire : « ajoute le colisage et le franco, cela évite un retour ». Le
    bon montre le colisage des lignes où il est connu, une case vide ailleurs, et le franco
    atteint. Sans aucun colisage ni franco, il ne montre ni colonne ni phrase."""
    from pharma2a import courrier
    from pharma2a.dictionnaire import valider
    doc = json.loads((EXEMPLES / "commande.json").read_text(encoding="utf-8"))
    assert valider("commande", doc) == []
    doc["franco_ht"] = 300
    objet, texte, html = courrier.commande(doc)
    assert "Franco de port : 300 € HT, atteint par cette commande." in texte
    assert "Colis" in texte and "Colisage" in html and "franco atteint" in html and "par 6" not in html
    ligne = next(l for l in texte.splitlines() if "3400000000011" in l)
    assert ligne.split()[:2] == ["6", "6"]
    ligne = next(l for l in texte.splitlines() if "3400000000028" in l)
    assert ligne.split()[:2] == ["21", "3400000000028"]      # colisage inconnu : case vide
    doc["franco_ht"] = 249.5
    assert "249,50 € HT" in courrier.commande(doc)[1]
    for l in doc["lignes"]:
        l["colisage"] = None
    doc["franco_ht"] = None
    _, texte, html = courrier.commande(doc)
    assert "Colis" not in texte and "Colisage" not in html and "Franco" not in texte
    doc["lignes"][0]["colisage"] = 0
    assert valider("commande", doc) != []
    doc["lignes"][0]["colisage"] = 6; doc["franco_ht"] = -1
    assert valider("commande", doc) != []


def test_l_onglet_commande_de_l_excel_porte_le_colisage():
    from pharma2a import courrier
    from openpyxl import load_workbook
    import io
    doc = json.loads((EXEMPLES / "commande.json").read_text(encoding="utf-8"))
    doc["franco_ht"] = 300
    rayon = json.loads((EXEMPLES / "etat_de_rayon.json").read_text(encoding="utf-8"))
    wb = load_workbook(io.BytesIO(courrier.etat_de_rayon_xlsx(rayon, doc)))
    ws = wb["Commande"]
    assert [c.value for c in ws[4]][:4] == ["Qté commandée", "Colisage", "Code", "Désignation"]
    assert [c.value for c in ws[5]][:3] == [6, 6, "3400000000011"]
    assert ws[6][1].value is None
    assert ws["A2"].value.startswith("Franco de port : 300 € HT")


def test_le_lien_de_la_page_de_reponse_est_dans_le_mail():
    """23/10 : la commande peut porter l'adresse d'une page de réponse ; le mail la montre en
    bouton et en clair, et rappelle que répondre au message suffit toujours."""
    from pharma2a import courrier
    from pharma2a.dictionnaire import valider
    doc = json.loads((EXEMPLES / "commande.json").read_text(encoding="utf-8"))
    doc["page_reponse"] = "https://exemple.test/reponse/" + "a" * 64
    assert valider("commande", doc) == []
    _, texte, html = courrier.commande(doc)
    assert doc["page_reponse"] in texte and "répondez simplement" in texte
    # une commande ferme se confirme ; « Répondre sur la page » reste à la proposition
    assert f'href="{doc["page_reponse"]}"' in html and "Confirmer la commande" in html
    doc["page_reponse"] = "http://exemple.test/x"
    assert valider("commande", doc) != []      # https seulement


def test_fermer_une_tache_la_sort_des_reponses_attendues(client):
    """L'officine ferme une tâche (commande remplacée ou annulée) : elle passe « annulée »,
    se relit telle quelle, et la relecture des réponses ne la compte plus."""
    import asyncio
    from pharma2a.relecture import taches_en_cours
    r = client.post("/message:send", json=envoi("commande", exemple("commande")), headers=AUTH)
    tache = r.json()["task"]
    assert tache["status"]["state"] == "TASK_STATE_WORKING"
    r2 = client.post(f"/tasks/{tache['id']}:cancel", json={}, headers=AUTH)
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"]["state"] == "TASK_STATE_CANCELED", r2.json()
    r3 = client.get(f"/tasks/{tache['id']}", headers=AUTH)
    assert r3.json()["status"]["state"] == "TASK_STATE_CANCELED"
    # sans jeton, la porte reste fermée
    assert client.post(f"/tasks/{tache['id']}:cancel", json={}).status_code in (401, 403)
    store = client.app.state.annuaire.magasin if hasattr(client.app.state, "annuaire") else None
    if store is not None:
        assert tache["id"] not in {t.id for t in asyncio.run(taches_en_cours(store))}


# Le guichet : la fiche du laboratoire se lit sans clé, comme une vitrine ; par A2A elle
# revient en artefact. Sans fiche, l'agent le dit. Une fiche hors dictionnaire n'est pas servie.

def demande_fiche():
    m = new_data_message({"version": "1", "demande": "fiche_fournisseur"}, media_type="application/json", role=Role.ROLE_USER)
    m.message_id = str(uuid.uuid4())
    return MessageToDict(SendMessageRequest(message=m))


def test_sans_fiche_le_guichet_est_ferme(client):
    r = client.get("/fiche_fournisseur.json")
    assert r.status_code == 404 and "fiche" in r.json()["erreur"]
    r = client.post("/message:send", json=demande_fiche(), headers=AUTH)
    assert r.status_code == 200
    assert r.json()["task"]["status"]["state"] == "TASK_STATE_FAILED"
    assert not list(client.sorties.glob("*.eml"))


@pytest.fixture
def client_fiche(tmp_path):
    f = tmp_path / "fiche.json"
    f.write_text((EXEMPLES / "fiche_fournisseur.json").read_text(encoding="utf-8"), encoding="utf-8")
    config = Config(jeton=JETON, fournisseur="LABO UN", brouillon=True, sorties=tmp_path / "sorties", fiche=str(f))
    with TestClient(creer_app(config)) as c:
        c.fiche = f; c.sorties = tmp_path / "sorties"
        yield c


def test_la_fiche_se_lit_sans_cle(client_fiche):
    r = client_fiche.get("/fiche_fournisseur.json")
    assert r.status_code == 200, r.text
    fiche = r.json()
    assert fiche["fournisseur"]["nom"] == "LABORATOIRE EXEMPLE"
    assert fiche["conditions"]["assiette_franco"] == "net_remise"
    assert [g["statut"] for g in fiche["gamme"]] == ["actif", "actif", "arrete", "nouveau", "sur_commande"]
    assert "prix" not in r.text.lower() and "remise" not in r.text.lower().replace("net_remise", "")
    assert r.headers["cache-control"].startswith("public") and r.headers.get("etag")
    assert client_fiche.get("/fiche_fournisseur.json", headers={"If-None-Match": r.headers["etag"]}).status_code == 304
    assert client_fiche.head("/fiche_fournisseur.json").status_code == 200
    # le reste reste fermé
    assert client_fiche.get("/extendedAgentCard").status_code == 401


def test_la_fiche_revient_en_artefact_par_a2a(client_fiche):
    r = client_fiche.post("/message:send", json=demande_fiche(), headers=AUTH)
    assert r.status_code == 200, r.text
    tache = r.json()["task"]
    assert tache["status"]["state"] == "TASK_STATE_COMPLETED", tache
    art = tache["artifacts"][0]
    assert art["name"] == "fiche_fournisseur"
    part = art["parts"][0]
    assert part["mediaType"] == MEDIA["fiche_fournisseur"]
    assert part["data"]["fournisseur"]["nom"] == "LABORATOIRE EXEMPLE"
    assert len(part["data"]["gamme"]) == 5
    from pharma2a.dictionnaire import EXTENSION
    assert art["extensions"] == [EXTENSION]
    assert not list(client_fiche.sorties.glob("*.eml"))      # rien ne part au laboratoire


# --- les prix du fournisseur

def test_la_vitrine_masque_le_tarif_et_l_officine_authentifiee_le_recoit(client_fiche):
    public = client_fiche.get("/fiche_fournisseur.json").json()
    assert "tarif" not in public and all("prix_tarif_ht" not in g for g in public["gamme"])
    assert [d["nature"] for d in public["documents"]] == ["conditions_generales"]   # le PDF du tarif non plus
    assert public["gamme"][0]["taux_tva"] == 5.5          # la TVA n'est pas un prix : elle reste
    r = client_fiche.post("/message:send", json=demande_fiche(), headers=AUTH).json()["task"]
    data = r["artifacts"][0]["parts"][0]["data"]
    assert data["tarif"] == {"date": "2026-01-01", "devise": "EUR"}
    assert [g["prix_tarif_ht"] for g in data["gamme"]][:2] == [9.9, 7.5]


def test_un_fournisseur_peut_rendre_son_tarif_public(tmp_path):
    f = tmp_path / "fiche.json"
    f.write_text((EXEMPLES / "fiche_fournisseur.json").read_text(encoding="utf-8"), encoding="utf-8")
    config = Config(jeton=JETON, fournisseur="LABO UN", brouillon=True, sorties=tmp_path / "s", fiche=str(f), fiche_tarif_public=True)
    with TestClient(creer_app(config)) as c:
        public = c.get("/fiche_fournisseur.json").json()
    assert public["tarif"]["date"] == "2026-01-01" and public["gamme"][0]["prix_tarif_ht"] == 9.9
    assert [d["nature"] for d in public["documents"]] == ["tarif", "conditions_generales"]


def test_une_remise_n_entre_pas_dans_la_fiche(client_fiche):
    import time
    time.sleep(0.01)
    doc = json.loads(client_fiche.fiche.read_text(encoding="utf-8"))
    doc["gamme"][0]["remise_pct"] = 37.5
    client_fiche.fiche.write_text(json.dumps(doc), encoding="utf-8")
    r = client_fiche.get("/fiche_fournisseur.json")
    # la vitrine refuse sans répéter le détail : il citerait la remise
    assert r.status_code == 503 and "37.5" not in r.text and "remise_pct" not in r.text


def test_une_fiche_hors_dictionnaire_n_est_pas_servie(client_fiche):
    import time
    time.sleep(0.01)
    doc = json.loads(client_fiche.fiche.read_text(encoding="utf-8"))
    doc["gamme"][0]["prix_ht"] = 12.5
    client_fiche.fiche.write_text(json.dumps(doc), encoding="utf-8")
    r = client_fiche.get("/fiche_fournisseur.json")
    assert r.status_code == 503 and "12.5" not in r.text and "prix_ht" not in r.text
    r = client_fiche.post("/message:send", json=demande_fiche(), headers=AUTH)
    assert r.json()["task"]["status"]["state"] == "TASK_STATE_FAILED"
    # corrigée, elle est relue sans redémarrage
    time.sleep(0.01)
    del doc["gamme"][0]["prix_ht"]
    client_fiche.fiche.write_text(json.dumps(doc), encoding="utf-8")
    assert client_fiche.get("/fiche_fournisseur.json").status_code == 200


# Une commande n'est transmise qu'une fois : la même référence pour le même compte
# client rend la tâche déjà ouverte, même identifiant, même état, et rien ne repart. Deux
# comptes clients, c'est deux commandes. La proposition et la commande qui la suit partagent
# la référence, pas la nature. Une tâche refusée ou annulée n'a rien transmis : la référence revient.

def test_la_meme_commande_envoyee_deux_fois_n_est_transmise_qu_une_fois(client):
    r1 = client.post("/message:send", json=envoi("commande", exemple("commande")), headers=AUTH).json()["task"]
    r2 = client.post("/message:send", json=envoi("commande", exemple("commande")), headers=AUTH).json()["task"]
    assert r1["id"] == r2["id"] and r2["status"]["state"] == "TASK_STATE_WORKING"
    assert len(list(client.sorties.glob("*.eml"))) == 1
    # même par JSON-RPC
    corps = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": envoi("commande", exemple("commande"))}
    assert client.post("/", json=corps, headers=AUTH).json()["result"]["task"]["id"] == r1["id"]
    assert len(list(client.sorties.glob("*.eml"))) == 1


def test_la_meme_reference_pour_deux_officines_fait_deux_commandes(client):
    """Deux officines (compte, CIP, nom et adresse différents) sous la même référence : deux
    commandes. La même officine avec un autre numéro de compte (même CIP, même adresse) : la même."""
    a = exemple("commande")
    b = exemple("commande"); b["officine"].update(numero_compte_client="654321", cip_officine="2000002", nom="Pharmacie du Second Exemple")
    b["officine"]["adresse_livraison"].update(ligne1="9 avenue de l'Exemple")
    ta = client.post("/message:send", json=envoi("commande", a), headers=AUTH).json()["task"]
    tb = client.post("/message:send", json=envoi("commande", b), headers=AUTH).json()["task"]
    assert ta["id"] != tb["id"]
    assert len(list(client.sorties.glob("*.eml"))) == 2
    c = exemple("commande"); c["officine"]["numero_compte_client"] = "999999"
    assert client.post("/message:send", json=envoi("commande", c), headers=AUTH).json()["task"]["id"] == ta["id"]
    assert len(list(client.sorties.glob("*.eml"))) == 2


def test_la_proposition_puis_la_commande_sous_la_meme_reference_sont_deux_taches(client):
    p = exemple("commande"); p["nature"] = "proposition"; p["questions"] = ["franco", "colisage"]
    tp = client.post("/message:send", json=envoi("commande", p), headers=AUTH).json()["task"]
    tc = client.post("/message:send", json=envoi("commande", exemple("commande")), headers=AUTH).json()["task"]
    assert tp["id"] != tc["id"]
    assert len(list(client.sorties.glob("*.eml"))) == 2
    # la commande renvoyée rend la tâche de la commande, pas celle de la proposition
    assert client.post("/message:send", json=envoi("commande", exemple("commande")), headers=AUTH).json()["task"]["id"] == tc["id"]


def test_une_reference_refusee_puis_annulee_peut_revenir(client):
    doc = exemple("commande"); doc["lignes"][0]["prix_ht"] = 1
    assert client.post("/message:send", json=envoi("commande", doc), headers=AUTH).json()["task"]["status"]["state"] == "TASK_STATE_REJECTED"
    t1 = client.post("/message:send", json=envoi("commande", exemple("commande")), headers=AUTH).json()["task"]
    assert t1["status"]["state"] == "TASK_STATE_WORKING"
    client.post(f"/tasks/{t1['id']}:cancel", json={}, headers=AUTH)
    t2 = client.post("/message:send", json=envoi("commande", exemple("commande")), headers=AUTH).json()["task"]
    assert t2["id"] != t1["id"] and t2["status"]["state"] == "TASK_STATE_WORKING"
    assert len(list(client.sorties.glob("*.eml"))) == 2


def test_l_idempotence_tient_dans_le_magasin_sqlite(tmp_path):
    """Le magasin de production est SQLite : la clé se retrouve après un redémarrage."""
    base = tmp_path / "taches.sqlite"
    config = Config(jeton=JETON, fournisseur="LABO UN", brouillon=True, sorties=tmp_path / "sorties", base=str(base))
    with TestClient(creer_app(config)) as c:
        t1 = c.post("/message:send", json=envoi("commande", exemple("commande")), headers=AUTH).json()["task"]
    config = Config(jeton=JETON, fournisseur="LABO UN", brouillon=True, sorties=tmp_path / "sorties", base=str(base))
    with TestClient(creer_app(config)) as c:
        t2 = c.post("/message:send", json=envoi("commande", exemple("commande")), headers=AUTH).json()["task"]
    assert t1["id"] == t2["id"]
    assert len(list((tmp_path / "sorties").glob("*.eml"))) == 1


def test_deux_lignes_avec_le_meme_id_ligne_sont_refusees(client):
    doc = exemple("commande"); doc["lignes"][1]["id_ligne"] = doc["lignes"][0]["id_ligne"]
    t = client.post("/message:send", json=envoi("commande", doc), headers=AUTH).json()["task"]
    assert t["status"]["state"] == "TASK_STATE_REJECTED" and "id_ligne" in t["status"]["message"]["parts"][0]["text"]


def test_une_ligne_sans_cip13_part_avec_son_ean13(client):
    """Certains produits de parapharmacie n'ont pas de CIP 13 : la ligne porte son EAN 13 ou la
    référence du fournisseur, et le bon la montre avec ce code."""
    doc = exemple("commande")
    doc["lignes"][0] = {"id_ligne": "1", "ean13": "3600000000017", "libelle": "BROSSE A DENTS SOUPLE", "quantite": 12}
    doc["lignes"][1] = {"id_ligne": "2", "ref_fournisseur": "ACC-0042", "libelle": "PRESENTOIR DE COMPTOIR", "quantite": 1}
    t = client.post("/message:send", json=envoi("commande", doc), headers=AUTH).json()["task"]
    assert t["status"]["state"] == "TASK_STATE_WORKING", t["status"]
    eml = next(client.sorties.glob("*.eml")).read_bytes().decode("utf-8", "replace")
    assert "3600000000017" in eml and "ACC-0042" in eml


def test_un_message_sur_une_tache_ouverte_ne_retransmet_rien(client):
    """Reprendre une tâche par son identifiant, quel que soit le contenu du message, ne fait
    rien repartir : la tâche reste dans son état et le dit."""
    t1 = client.post("/message:send", json=envoi("commande", exemple("commande")), headers=AUTH).json()["task"]
    doc = exemple("commande"); doc["lignes"][0]["quantite"] = 60
    m = new_data_message(doc, media_type=MEDIA["commande"], role=Role.ROLE_USER)
    m.message_id = str(uuid.uuid4()); m.task_id = t1["id"]
    r = client.post("/message:send", json=MessageToDict(SendMessageRequest(message=m)), headers=AUTH)
    assert r.status_code == 200, r.text
    t2 = r.json()["task"]
    assert t2["id"] == t1["id"] and t2["status"]["state"] == "TASK_STATE_WORKING"
    assert "déjà ouverte" in t2["status"]["message"]["parts"][0]["text"]
    assert len(list(client.sorties.glob("*.eml"))) == 1


def test_un_document_mal_forme_est_refuse_pas_une_panne(client):
    for casse in (lambda d: d.update(lignes=None), lambda d: d.pop("lignes"), lambda d: d.update(officine="x"),
                  lambda d: d.update(lignes=[None]), lambda d: d.update(lignes=[])):
        doc = exemple("commande"); casse(doc)
        t = client.post("/message:send", json=envoi("commande", doc), headers=AUTH).json()["task"]
        assert t["status"]["state"] == "TASK_STATE_REJECTED", t["status"]
    assert not list(client.sorties.glob("*.eml"))


def test_une_reference_annulee_puis_renvoyee_le_dit_au_fournisseur(client):
    """L'officine ferme une tâche puis renvoie la même référence : c'est permis, et le second
    mail dit qu'il annule et remplace le premier."""
    t1 = client.post("/message:send", json=envoi("commande", exemple("commande")), headers=AUTH).json()["task"]
    r = client.post(f"/tasks/{t1['id']}:cancel", json={}, headers=AUTH).json()
    assert "Fermée par l'officine" in r["status"]["message"]["parts"][0]["text"]
    t2 = client.post("/message:send", json=envoi("commande", exemple("commande")), headers=AUTH).json()["task"]
    assert t2["id"] != t1["id"] and "remplacement de l'envoi précédent" in t2["status"]["message"]["parts"][0]["text"]
    emls = sorted(client.sorties.glob("*.eml"), key=lambda f: f.stat().st_mtime_ns)
    assert len(emls) == 2
    premier, second = (f.read_bytes().decode("utf-8", "replace") for f in emls)
    assert "annule et remplace" not in premier and "annule et remplace" in second


def test_le_mode_immediat_rend_la_tache_sans_attendre(client):
    from a2a.types import SendMessageConfiguration
    m = new_data_message(exemple("commande"), media_type=MEDIA["commande"], role=Role.ROLE_USER); m.message_id = str(uuid.uuid4())
    corps = MessageToDict(SendMessageRequest(message=m, configuration=SendMessageConfiguration(return_immediately=True)))
    r = client.post("/message:send", json=corps, headers=AUTH)
    assert r.status_code == 200, r.text
    t = r.json()["task"]
    assert t["status"]["state"] in ("TASK_STATE_SUBMITTED", "TASK_STATE_WORKING")
    import time
    for _ in range(50):
        if client.get(f"/tasks/{t['id']}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_WORKING":
            break
        time.sleep(0.05)
    assert client.get(f"/tasks/{t['id']}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_WORKING"
    assert len(list(client.sorties.glob("*.eml"))) == 1


def test_une_reference_echouee_puis_renvoyee_le_dit_aussi_au_fournisseur(tmp_path):
    """Le canal a levé une erreur, mais le mail a pu être accepté avant : rien ne repart tout seul.
    Une fois la tâche fermée par l'officine, le second envoi dit qu'il remplace le précédent,
    qu'il soit arrivé ou non."""
    from pharma2a.backends.mail import Backend
    config = Config(jeton=JETON, fournisseur="LABO UN", brouillon=True, sorties=tmp_path / "sorties")

    class Capricieux(Backend):
        pannes = 1
        def envoyer(self, *a, **k):
            if Capricieux.pannes:
                Capricieux.pannes -= 1
                raise ConnectionError("coupure pendant l'envoi")
            return super().envoyer(*a, **k)

    with TestClient(creer_app(config, backend=Capricieux(config))) as c:
        t1 = c.post("/message:send", json=envoi("commande", exemple("commande")), headers=AUTH).json()["task"]
        # l'issue est incertaine : la tâche attend une décision, rien ne repart tout seul
        assert t1["status"]["state"] == "TASK_STATE_INPUT_REQUIRED" and "incertaine" in json.dumps(t1)
        assert c.post("/message:send", json=envoi("commande", exemple("commande")), headers=AUTH).json()["task"]["id"] == t1["id"]
        assert not list((tmp_path / "sorties").glob("*.eml"))
        # l'officine a vérifié et ferme la tâche : le renvoi part, en disant qu'il remplace l'envoi incertain
        c.post(f"/tasks/{t1['id']}:cancel", json={}, headers=AUTH)
        t2 = c.post("/message:send", json=envoi("commande", exemple("commande")), headers=AUTH).json()["task"]
        assert t2["id"] != t1["id"] and t2["status"]["state"] == "TASK_STATE_WORKING"
        assert "issue était incertaine" in t2["status"]["message"]["parts"][0]["text"]
        eml = next((tmp_path / "sorties").glob("*.eml")).read_bytes().decode("utf-8", "replace")
        assert "Si vous avez d" in eml and "celui-ci le remplace" in eml and "annule et remplace" not in eml


def test_dans_un_annuaire_une_tache_sans_metadonnee_n_est_a_personne():
    from a2a.types import Task
    from pharma2a.serveur import Guichet
    g = Guichet.__new__(Guichet); g.agent = "labo-un"
    assert g._mienne(Task(id="x")) is False
    g.agent = ""
    assert g._mienne(Task(id="x")) is True


def test_le_bon_joint_le_rayon_par_identifiant_type():
    """Une référence fournisseur qui a les chiffres du CIP d'une autre ligne ne prend pas son
    stock : un CIP ne se compare qu'à un CIP."""
    from pharma2a import courrier
    from openpyxl import load_workbook
    import io
    doc = json.loads((EXEMPLES / "commande.json").read_text(encoding="utf-8"))
    doc["lignes"] = [{"id_ligne": "1", "cip13": "3400000000011", "libelle": "PROBIOTIQUE 7 SOUCHES STICKS 30", "quantite": 6},
                     {"id_ligne": "2", "ref_fournisseur": "3400000000011", "libelle": "PRESENTOIR", "quantite": 1}]
    rayon = json.loads((EXEMPLES / "etat_de_rayon.json").read_text(encoding="utf-8"))
    rayon["lignes"] = [{"cip13": "3400000000011", "libelle": "PROBIOTIQUE 7 SOUCHES STICKS 30", "stock": 5, "ventes_3_mois": 9, "couverture_jours": 50, "mois_a_zero_3_mois": 0}]
    doc["etat_de_rayon"] = rayon
    _, texte, _ = courrier.commande(doc)
    l1 = next(l for l in texte.splitlines() if "PROBIOTIQUE" in l and l.strip().startswith("6"))
    l2 = next(l for l in texte.splitlines() if "PRESENTOIR" in l)
    assert l1.split()[:3] == ["6", "9", "5"]
    # ni ventes ni stock pour le présentoir, et son code dit qu'il n'est pas un CIP
    assert l2.split()[:3] == ["1", "Réf.", "3400000000011"]
    ws = load_workbook(io.BytesIO(courrier.etat_de_rayon_xlsx(rayon, doc)))["Commande"]
    assert [c.value for c in ws[5]][:5] == [6, None, "3400000000011", "PROBIOTIQUE 7 SOUCHES STICKS 30", 5]
    assert [c.value for c in ws[6]][4] is None


def test_un_etat_de_rayon_vide_joint_a_la_commande_est_refuse(client):
    doc = exemple("commande"); doc["etat_de_rayon"] = {}
    t = client.post("/message:send", json=envoi("commande", doc), headers=AUTH).json()["task"]
    assert t["status"]["state"] == "TASK_STATE_REJECTED" and "etat_de_rayon" in t["status"]["message"]["parts"][0]["text"]
    assert not list(client.sorties.glob("*.eml"))


def test_la_jointure_typee_refuse_l_ambigu_et_le_contradictoire():
    from pharma2a.dictionnaire import indexer, retrouver
    a = {"cip13": "3400000000011", "ean13": "3600000000017", "libelle": "A"}
    b = {"cip13": "3400000000028", "libelle": "B"}
    index = indexer([a, b])
    assert retrouver(index, {"cip13": "3400000000011"}) is a
    assert retrouver(index, {"ean13": "3600000000017"}) is a
    assert retrouver(index, {"cip13": "3400000000011", "ean13": "3600000000017"}) is a
    assert retrouver(index, {"cip13": "3400000000028", "ean13": "3600000000017"}) is None     # se contredisent
    assert retrouver(index, {"ref_fournisseur": "3400000000011"}) is None                    # pas le même type
    index = indexer([a, {"cip13": "3400000000099", "ean13": "3600000000017", "libelle": "C"}])
    assert retrouver(index, {"ean13": "3600000000017"}) is None                              # porté par deux lignes
    assert retrouver(index, {"cip13": "3400000000011"}) is a


def test_deux_officines_homonymes_a_deux_adresses_sont_deux_comptes(client):
    a = exemple("commande"); a["officine"]["numero_compte_client"] = None; a["officine"]["cip_officine"] = None
    b = exemple("commande"); b["officine"]["numero_compte_client"] = None; b["officine"]["cip_officine"] = None
    b["officine"]["adresse_livraison"].update(ligne1="2 rue de l'Officine")      # même nom, même ville, autre rue
    ta = client.post("/message:send", json=envoi("commande", a), headers=AUTH).json()["task"]
    tb = client.post("/message:send", json=envoi("commande", b), headers=AUTH).json()["task"]
    assert ta["id"] != tb["id"] and len(list(client.sorties.glob("*.eml"))) == 2
    assert client.post("/message:send", json=envoi("commande", a), headers=AUTH).json()["task"]["id"] == ta["id"]


def test_un_jeton_non_ascii_est_refuse_proprement():
    """Un client qui envoie des octets non ASCII dans le jeton reçoit 401, pas une erreur 500 :
    la porte compare des octets. Le client de test refuse d'encoder un tel en-tête, on parle
    donc à la porte directement, en ASGI."""
    import asyncio
    from pharma2a.serveur import Porte
    async def appli(scope, receive, send):
        raise AssertionError("la porte aurait dû refuser")
    porte = Porte(appli, "jeton-de-test")
    envoyes = []
    async def send(message): envoyes.append(message)
    async def receive(): return {"type": "http.request", "body": b"", "more_body": False}
    scope = {"type": "http", "method": "POST", "path": "/message:send", "query_string": b"",
             "headers": [(b"authorization", "Bearer jet\u00e9n".encode("latin-1")), (b"a2a-version", b"1.0")]}
    asyncio.run(porte(scope, receive, send))
    assert envoyes[0]["type"] == "http.response.start" and envoyes[0]["status"] == 401


def test_l_excel_n_interprete_pas_une_formule():
    from pharma2a import courrier
    from openpyxl import load_workbook
    import io
    doc = json.loads((EXEMPLES / "commande.json").read_text(encoding="utf-8"))
    doc["lignes"][0]["libelle"] = "=1+1"
    rayon = json.loads((EXEMPLES / "etat_de_rayon.json").read_text(encoding="utf-8"))
    rayon["lignes"][0]["libelle"] = "=HYPERLINK(\"x\")"
    wb = load_workbook(io.BytesIO(courrier.etat_de_rayon_xlsx(rayon, doc)))
    assert wb["Commande"]["D5"].value == "=1+1" and wb["Commande"]["D5"].data_type == "s"
    ws = wb["État de rayon"]
    cell = next(c for row in ws.iter_rows() for c in row if c.value == "=HYPERLINK(\"x\")")
    assert cell.data_type == "s"


def test_une_repetition_qui_ajoute_le_compte_client_reste_la_meme_commande(client):
    a = exemple("commande"); a["officine"]["numero_compte_client"] = None
    b = exemple("commande")                                            # même CIP, le compte en plus
    ta = client.post("/message:send", json=envoi("commande", a), headers=AUTH).json()["task"]
    tb = client.post("/message:send", json=envoi("commande", b), headers=AUTH).json()["task"]
    assert ta["id"] == tb["id"] and len(list(client.sorties.glob("*.eml"))) == 1


def test_une_repetition_reconnue_apprend_les_identites(client):
    """Premier envoi avec le CIP seul ; répétition qui ajoute le compte client, reconnue ;
    troisième envoi avec le compte seul : toujours la même commande. Un quatrième avec une
    adresse de livraison corrigée est la même officine mais plus la même commande : il est refusé en clair, rien ne repart."""
    a = exemple("commande"); a["officine"]["numero_compte_client"] = None
    b = exemple("commande")
    c = exemple("commande"); c["officine"]["cip_officine"] = None
    d = exemple("commande"); d["officine"]["cip_officine"] = None; d["officine"]["adresse_livraison"]["ligne1"] = "3 rue de la Pharmacie"
    ta = client.post("/message:send", json=envoi("commande", a), headers=AUTH).json()["task"]["id"]
    assert client.post("/message:send", json=envoi("commande", b), headers=AUTH).json()["task"]["id"] == ta
    assert client.post("/message:send", json=envoi("commande", c), headers=AUTH).json()["task"]["id"] == ta
    td = client.post("/message:send", json=envoi("commande", d), headers=AUTH).json()["task"]
    assert td["id"] != ta and td["status"]["state"] == "TASK_STATE_REJECTED"
    assert len(list(client.sorties.glob("*.eml"))) == 1


def test_une_repetition_mal_formee_n_apprend_rien_a_la_tache(client):
    a = exemple("commande"); a["officine"]["numero_compte_client"] = None
    ta = client.post("/message:send", json=envoi("commande", a), headers=AUTH).json()["task"]["id"]
    casse = exemple("commande"); casse["officine"]["numero_compte_client"] = "777777"; casse["lignes"] = [None]
    t = client.post("/message:send", json=envoi("commande", casse), headers=AUTH).json()["task"]
    assert t["status"]["state"] == "TASK_STATE_REJECTED" and t["id"] != ta
    # une autre officine, qui n'a que ce compte client : une autre commande, pas la première
    autre = exemple("commande"); autre["officine"].update(numero_compte_client="777777", cip_officine=None, nom="Pharmacie du Second Exemple")
    autre["officine"]["adresse_livraison"]["ligne1"] = "9 avenue de l'Exemple"
    tb = client.post("/message:send", json=envoi("commande", autre), headers=AUTH).json()["task"]
    assert tb["id"] != ta and tb["status"]["state"] == "TASK_STATE_WORKING"
    assert len(list(client.sorties.glob("*.eml"))) == 2


def test_une_repetition_avec_un_rayon_vide_ou_des_id_ligne_en_double_est_refusee_sans_apprendre(client):
    a = exemple("commande"); a["officine"]["numero_compte_client"] = None
    ta = client.post("/message:send", json=envoi("commande", a), headers=AUTH).json()["task"]["id"]
    for casse in (lambda d: d.update(etat_de_rayon={}), lambda d: d["lignes"][1].update(id_ligne="1")):
        doc = exemple("commande"); doc["officine"]["numero_compte_client"] = "777777"; casse(doc)
        t = client.post("/message:send", json=envoi("commande", doc), headers=AUTH).json()["task"]
        assert t["status"]["state"] == "TASK_STATE_REJECTED" and t["id"] != ta
    autre = exemple("commande"); autre["officine"].update(numero_compte_client="777777", cip_officine=None, nom="Pharmacie du Second Exemple")
    autre["officine"]["adresse_livraison"]["ligne1"] = "9 avenue de l'Exemple"
    assert client.post("/message:send", json=envoi("commande", autre), headers=AUTH).json()["task"]["id"] != ta


def test_une_meme_reference_avec_un_autre_contenu_est_refusee_en_clair(client):
    """Une relance ou une commande corrigée sous la même référence, pendant que la tâche est
    encore ouverte : le pont ne rend pas l'ancienne tâche en silence, il refuse et dit quoi faire."""
    t1 = client.post("/message:send", json=envoi("commande", exemple("commande")), headers=AUTH).json()["task"]
    doc = exemple("commande"); doc["lignes"][0]["quantite"] = 24
    t2 = client.post("/message:send", json=envoi("commande", doc), headers=AUTH).json()["task"]
    assert t2["status"]["state"] == "TASK_STATE_REJECTED" and t2["id"] != t1["id"]
    texte = t2["status"]["message"]["parts"][0]["text"]
    assert t1["id"] in texte and "fermez-la" in texte and "Rien n'est parti" in texte
    assert len(list(client.sorties.glob("*.eml"))) == 1
    doc = exemple("commande"); doc["date"] = "2026-10-16"; doc["message"] = "merci"; doc["page_reponse"] = "https://exemple.test/r/" + "b" * 64
    assert client.post("/message:send", json=envoi("commande", doc), headers=AUTH).json()["task"]["id"] == t1["id"]
    client.post(f"/tasks/{t1['id']}:cancel", json={}, headers=AUTH)
    doc = exemple("commande"); doc["lignes"][0]["quantite"] = 24
    t3 = client.post("/message:send", json=envoi("commande", doc), headers=AUTH).json()["task"]
    assert t3["status"]["state"] == "TASK_STATE_WORKING" and len(list(client.sorties.glob("*.eml"))) == 2


def test_la_commande_ferme_apres_la_proposition_fermee_le_dit(client):
    p = exemple("commande"); p["nature"] = "proposition"; p["questions"] = ["colisage"]
    tp = client.post("/message:send", json=envoi("commande", p), headers=AUTH).json()["task"]
    client.post(f"/tasks/{tp['id']}:cancel", json={}, headers=AUTH)
    tc = client.post("/message:send", json=envoi("commande", exemple("commande")), headers=AUTH).json()["task"]
    assert "suite de la proposition" in tc["status"]["message"]["parts"][0]["text"]
    eml = sorted(client.sorties.glob("*.eml"), key=lambda f: f.stat().st_mtime_ns)[-1].read_bytes().decode("utf-8", "replace")
    assert "fait suite" in eml and "annule et remplace" not in eml


def test_un_client_ne_pose_pas_la_metadonnee_du_guichet(client):
    doc = exemple("commande")
    m = new_data_message(doc, media_type=MEDIA["commande"], role=Role.ROLE_USER); m.message_id = str(uuid.uuid4())
    m.metadata.update({"pharma2a.remplace": "x", "pharma2a.remplace_motif": "fermee", "pharma2a.doublon_different": "y"})
    t = client.post("/message:send", json=MessageToDict(SendMessageRequest(message=m)), headers=AUTH).json()["task"]
    assert t["status"]["state"] == "TASK_STATE_WORKING"
    eml = next(client.sorties.glob("*.eml")).read_bytes().decode("utf-8", "replace")
    assert "annule et remplace" not in eml and "remplacement" not in t["status"]["message"]["parts"][0]["text"]


# Le type de la réponse se négocie : application/json par défaut (A2A 1.0, section
# 11.1, et le kit de conformité), application/a2a+json pour le client qui le demande
def test_le_type_de_la_reponse_suit_ce_que_le_client_accepte(client):
    r = client.post("/message:send", json=envoi("commande", exemple("commande")), headers=AUTH)
    assert r.headers["content-type"].startswith("application/json")
    tid = r.json()["task"]["id"]
    r = client.get(f"/tasks/{tid}", headers={**AUTH, "Accept": "application/a2a+json"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/a2a+json")
    assert r.json()["id"] == tid
    r = client.get(f"/tasks/{tid}", headers={**AUTH, "Accept": "application/json"})
    assert r.headers["content-type"].startswith("application/json")



# Des lignes qui ne sont pas une liste sont refusées en clair, pas une panne
def test_des_lignes_mal_formees_sont_refusees_en_clair(client):
    for lignes in (1, True, "x"):
        doc = exemple("commande"); doc["lignes"] = lignes
        r = client.post("/message:send", json=envoi("commande", doc), headers=AUTH)
        assert r.status_code == 200, r.text
        t = r.json()["task"]
        assert t["status"]["state"] == "TASK_STATE_REJECTED", t["status"]
