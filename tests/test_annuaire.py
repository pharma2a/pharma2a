"""L'annuaire : plusieurs agents dans un processus, chacun sous son chemin avec son jeton
et son destinataire ; un agent ajouté au fichier est servi sans redémarrage."""
import json
import os
import time
import uuid
import pytest
from starlette.testclient import TestClient
from google.protobuf.json_format import MessageToDict
from a2a.helpers import new_data_message
from a2a.types import SendMessageRequest, Role
from pharma2a.config import Config
from pharma2a.serveur import creer_app
from pharma2a.dictionnaire import MEDIA
from tests.test_agent import EXEMPLES

V = {"A2A-Version": "1.0"}


def ecrire(chemin, agents):
    chemin.write_text(json.dumps({"agents": agents}), encoding="utf-8")
    os.utime(chemin, None)


@pytest.fixture
def annuaire(tmp_path):
    f = tmp_path / "annuaire.json"
    fiche = tmp_path / "fiche-labo-un.json"
    fiche.write_text((EXEMPLES / "fiche_fournisseur.json").read_text(encoding="utf-8"), encoding="utf-8")
    ecrire(f, [{"id": "labo-un", "fournisseur": "LABO UN", "jeton": "jeton-a", "destinataire": "adv@labo-un.example", "copie": "delegue@labo-un.example", "fiche": str(fiche)},
               {"id": "labo-deux", "fournisseur": "LABO DEUX", "jeton": "jeton-b", "destinataire": "commandes@labo-deux.example"}])
    config = Config(annuaire=str(f), brouillon=True, sorties=tmp_path / "sorties", url="http://127.0.0.1:8765", relecture_minutes=0)
    with TestClient(creer_app(config)) as c:
        c.fichier = f; c.sorties = tmp_path / "sorties"
        yield c


def envoi(doc):
    m = new_data_message(doc, media_type=MEDIA["commande"], role=Role.ROLE_USER); m.message_id = str(uuid.uuid4())
    return MessageToDict(SendMessageRequest(message=m))


def test_chaque_agent_a_sa_carte_et_son_adresse(annuaire):
    a = annuaire.get("/labo-un/.well-known/agent-card.json").json()
    b = annuaire.get("/labo-deux/.well-known/agent-card.json").json()
    assert "LABO UN" in a["name"] and "LABO DEUX" in b["name"]
    assert a["supportedInterfaces"][0]["url"] == "http://127.0.0.1:8765/labo-un"
    assert annuaire.get("/inconnu/.well-known/agent-card.json").status_code == 404


def test_le_jeton_d_un_agent_ne_vaut_pas_pour_l_autre(annuaire):
    assert annuaire.get("/labo-un/extendedAgentCard", headers={"Authorization": "Bearer jeton-a", **V}).status_code == 200
    assert annuaire.get("/labo-deux/extendedAgentCard", headers={"Authorization": "Bearer jeton-a", **V}).status_code == 401
    assert annuaire.get("/labo-deux/extendedAgentCard", headers={"Authorization": "Bearer jeton-b", **V}).status_code == 200


def test_la_commande_part_au_destinataire_de_l_agent(annuaire):
    doc = json.loads((EXEMPLES / "commande.json").read_text(encoding="utf-8"))
    r = annuaire.post("/labo-un/message:send", json=envoi(doc), headers={"Authorization": "Bearer jeton-a", **V})
    assert r.status_code == 200, r.text
    assert r.json()["task"]["status"]["state"] == "TASK_STATE_WORKING"
    eml = next(annuaire.sorties.glob("*.eml")).read_bytes().decode("utf-8", "replace")
    assert "To: adv@labo-un.example" in eml and "Cc: delegue@labo-un.example" in eml
    # la tâche se relit sous le même chemin, avec le même jeton
    tid = r.json()["task"]["id"]
    assert annuaire.get(f"/labo-un/tasks/{tid}", headers={"Authorization": "Bearer jeton-a", **V}).status_code == 200


def test_un_agent_ajoute_au_fichier_est_servi_sans_redemarrage(annuaire):
    assert annuaire.get("/labo-trois/.well-known/agent-card.json").status_code == 404
    time.sleep(0.01)
    agents = json.loads(annuaire.fichier.read_text())["agents"]
    agents.append({"id": "labo-trois", "fournisseur": "LABO TROIS", "jeton": "jeton-c", "destinataire": "adv@labo-trois.example"})
    ecrire(annuaire.fichier, agents)
    r = annuaire.get("/labo-trois/.well-known/agent-card.json")
    assert r.status_code == 200 and "LABO TROIS" in r.json()["name"]


def test_un_jeton_change_dans_le_fichier_s_applique(annuaire):
    time.sleep(0.01)
    agents = json.loads(annuaire.fichier.read_text())["agents"]
    agents[1]["jeton"] = "jeton-b2"
    ecrire(annuaire.fichier, agents)
    assert annuaire.get("/labo-deux/extendedAgentCard", headers={"Authorization": "Bearer jeton-b", **V}).status_code == 401
    assert annuaire.get("/labo-deux/extendedAgentCard", headers={"Authorization": "Bearer jeton-b2", **V}).status_code == 200


def test_chaque_agent_a_sa_fiche_ou_pas(annuaire):
    r = annuaire.get("/labo-un/fiche_fournisseur.json")
    assert r.status_code == 200 and r.json()["fournisseur"]["nom"] == "LABORATOIRE EXEMPLE"
    assert annuaire.get("/labo-deux/fiche_fournisseur.json").status_code == 404


def test_les_taches_d_un_agent_ne_se_voient_pas_depuis_l_autre(annuaire):
    """Le magasin est partagé, pas les tâches : un agent ne rend, n'annule ni ne liste celles
    d'un autre, et la même commande chez deux agents fait deux tâches."""
    doc = json.loads((EXEMPLES / "commande.json").read_text(encoding="utf-8"))
    a = {"Authorization": "Bearer jeton-a", **V}; b = {"Authorization": "Bearer jeton-b", **V}
    tid = annuaire.post("/labo-un/message:send", json=envoi(doc), headers=a).json()["task"]["id"]
    assert annuaire.get(f"/labo-un/tasks/{tid}", headers=a).status_code == 200
    assert annuaire.get(f"/labo-deux/tasks/{tid}", headers=b).status_code == 404
    assert annuaire.post(f"/labo-deux/tasks/{tid}:cancel", json={}, headers=b).status_code == 404
    assert tid not in {t["id"] for t in annuaire.get("/labo-deux/tasks", headers=b).json().get("tasks", [])}
    assert tid in {t["id"] for t in annuaire.get("/labo-un/tasks", headers=a).json().get("tasks", [])}
    # même compte, même référence, mais chez l'autre agent : une autre commande
    t2 = annuaire.post("/labo-deux/message:send", json=envoi(doc), headers=b).json()["task"]
    assert t2["id"] != tid and t2["status"]["state"] == "TASK_STATE_WORKING"
    assert len(list(annuaire.sorties.glob("*.eml"))) == 2
    # et chez le même agent, la même : la tâche déjà ouverte
    assert annuaire.post("/labo-un/message:send", json=envoi(doc), headers=a).json()["task"]["id"] == tid


def test_toute_tache_porte_son_agent(annuaire):
    """La fiche et l'état de rayon aussi : aucune tâche d'un agent ne se lit depuis l'autre."""
    a = {"Authorization": "Bearer jeton-a", **V}; b = {"Authorization": "Bearer jeton-b", **V}
    m = new_data_message({"version": "1", "demande": "fiche_fournisseur"}, media_type="application/json", role=Role.ROLE_USER); m.message_id = str(uuid.uuid4())
    tf = annuaire.post("/labo-un/message:send", json=MessageToDict(SendMessageRequest(message=m)), headers=a).json()["task"]
    assert tf["status"]["state"] == "TASK_STATE_COMPLETED"
    rayon = json.loads((EXEMPLES / "etat_de_rayon.json").read_text(encoding="utf-8"))
    m = new_data_message(rayon, media_type=MEDIA["etat_de_rayon"], role=Role.ROLE_USER); m.message_id = str(uuid.uuid4())
    tr = annuaire.post("/labo-un/message:send", json=MessageToDict(SendMessageRequest(message=m)), headers=a).json()["task"]
    assert tr["status"]["state"] == "TASK_STATE_COMPLETED"
    for tid in (tf["id"], tr["id"]):
        assert annuaire.get(f"/labo-un/tasks/{tid}", headers=a).status_code == 200
        assert annuaire.get(f"/labo-deux/tasks/{tid}", headers=b).status_code == 404


@pytest.mark.parametrize("contenu", ["null", "[]", '{"agents": [null]}', '{"agents": "labo-un"}'])
def test_un_annuaire_mal_forme_ne_laisse_pas_les_anciens_jetons_actifs(annuaire, contenu):
    h = {"Authorization": "Bearer jeton-a", **V}
    assert annuaire.get("/labo-un/extendedAgentCard", headers=h).status_code == 200
    time.sleep(0.01)
    annuaire.fichier.write_text(contenu, encoding="utf-8"); os.utime(annuaire.fichier, None)
    assert annuaire.get("/labo-un/extendedAgentCard", headers=h).status_code == 404
    assert annuaire.get("/labo-un/extendedAgentCard", headers=h).status_code == 404


def test_une_adresse_inconnue_ne_donne_pas_la_liste_des_agents(annuaire):
    for chemin in ("/", "/inconnu", "/inconnu/tasks"):
        r = annuaire.get(chemin)
        assert r.status_code == 404 and "labo-un" not in r.text and "labo-deux" not in r.text
