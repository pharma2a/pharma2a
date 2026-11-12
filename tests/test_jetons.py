"""Les jetons des officines : chaque jeton est rattaché à un compte client, une commande d'un
autre compte est refusée, une officine ne voit que ses tâches, un jeton révoqué est refusé
sans redémarrage. Et le nom fiche_fournisseur, partout."""
import copy
import pathlib
import subprocess
import sys
import time
import pytest
from starlette.testclient import TestClient
from pharma2a import jetons
from pharma2a.config import Config
from pharma2a.serveur import creer_app
from pharma2a.dictionnaire import MEDIA, NOMS
from tests.test_agent import envoi, exemple

RACINE = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def agent(tmp_path):
    registre = tmp_path / "jetons.json"
    a = jetons.creer(registre, "123456", "Pharmacie de l'Exemple")       # le compte de l'exemple
    b = jetons.creer(registre, "999999", "Autre officine")
    config = Config(jetons=str(registre), fournisseur="LABO UN", brouillon=True, sorties=tmp_path / "sorties")
    with TestClient(creer_app(config)) as c:
        c.registre, c.a, c.b, c.sorties = registre, a, b, tmp_path / "sorties"
        yield c


def h(jeton):
    return {"Authorization": f"Bearer {jeton}", "A2A-Version": "1.0"}


def test_le_registre_ne_garde_que_l_empreinte(agent):
    texte = agent.registre.read_text()
    assert agent.a not in texte and agent.b not in texte
    assert jetons.empreinte(agent.a) in texte
    assert agent.a.startswith("p2a_") and len(agent.a) > 40


def test_jeton_absent(agent):
    assert agent.post("/message:send", json=envoi("commande", exemple("commande"))).status_code == 401


def test_jeton_invalide(agent):
    r = agent.post("/message:send", json=envoi("commande", exemple("commande")), headers=h("p2a_inconnu"))
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")


def test_jeton_valide_de_son_compte(agent):
    r = agent.post("/message:send", json=envoi("commande", exemple("commande")), headers=h(agent.a))
    assert r.status_code == 200 and r.json()["task"]["status"]["state"] == "TASK_STATE_WORKING"


def test_jeton_valide_avec_un_autre_compte_client(agent):
    r = agent.post("/message:send", json=envoi("commande", exemple("commande")), headers=h(agent.b))
    assert r.status_code == 400, r.text
    assert "compte client" in r.text
    assert not list(agent.sorties.glob("*.eml"))


def test_une_commande_sans_compte_client_est_refusee_a_un_jeton_rattache(agent):
    doc = copy.deepcopy(exemple("commande"))
    doc["officine"]["numero_compte_client"] = None
    r = agent.post("/message:send", json=envoi("commande", doc), headers=h(agent.a))
    assert r.status_code == 400


def test_le_compte_se_compare_sans_casse_ni_espaces(tmp_path):
    registre = tmp_path / "j.json"
    j = jetons.creer(registre, " ab  12 ")
    config = Config(jetons=str(registre), brouillon=True, sorties=tmp_path / "s")
    doc = copy.deepcopy(exemple("commande"))
    doc["officine"]["numero_compte_client"] = "AB 12"
    with TestClient(creer_app(config)) as c:
        assert c.post("/message:send", json=envoi("commande", doc), headers=h(j)).status_code == 200


def test_une_officine_ne_voit_pas_les_taches_d_une_autre(agent):
    t = agent.post("/message:send", json=envoi("commande", exemple("commande")), headers=h(agent.a)).json()["task"]
    assert agent.get(f"/tasks/{t['id']}", headers=h(agent.a)).status_code == 200
    assert agent.get(f"/tasks/{t['id']}", headers=h(agent.b)).status_code == 404
    assert agent.post(f"/tasks/{t['id']}:cancel", headers=h(agent.b)).status_code == 404
    liste_b = agent.get("/tasks", headers=h(agent.b)).json()
    assert not liste_b.get("tasks")
    liste_a = agent.get("/tasks", headers=h(agent.a)).json()
    assert [x["id"] for x in liste_a["tasks"]] == [t["id"]]


def test_la_carte_etendue_n_est_servie_qu_authentifie(agent):
    assert agent.get("/extendedAgentCard").status_code == 401
    assert agent.get("/extendedAgentCard", headers=h("p2a_inconnu")).status_code == 401
    assert agent.get("/extendedAgentCard", headers=h(agent.a)).status_code == 200
    assert agent.get("/.well-known/agent-card.json").status_code == 200


def test_un_jeton_revoque_est_refuse_sans_redemarrage(agent):
    assert agent.get("/extendedAgentCard", headers=h(agent.b)).status_code == 200
    time.sleep(0.01)
    assert jetons.revoquer(agent.registre, compte="999999") == 1
    assert agent.get("/extendedAgentCard", headers=h(agent.b)).status_code == 401
    assert agent.get("/extendedAgentCard", headers=h(agent.a)).status_code == 200


def test_un_registre_illisible_ne_laisse_passer_personne(agent):
    time.sleep(0.01)
    agent.registre.write_text("{pas du json")
    assert agent.get("/extendedAgentCard", headers=h(agent.a)).status_code == 401


def test_la_ligne_de_commande_cree_liste_et_revoque(tmp_path):
    registre = tmp_path / "r.json"
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(RACINE)}
    def run(*a):
        return subprocess.run([sys.executable, "-m", "pharma2a", "jeton", *a, "--fichier", str(registre)],
                              capture_output=True, text=True, env=env, cwd=RACINE)
    r = run("creer", "--compte", "C-1", "--nom", "Officine Une")
    assert r.returncode == 0, r.stderr
    j = r.stdout.strip()
    assert j.startswith("p2a_") and jetons.Registre(registre).compte_de(j) == "C-1"
    assert "C-1" in run("lister").stdout and j not in run("lister").stdout
    assert "1 jeton" in run("revoquer", "--compte", "C-1").stdout
    assert jetons.Registre(registre).compte_de(j) is None


def test_le_jeton_seul_rattache_a_un_compte(tmp_path):
    config = Config(jeton="seul", compte_client="999999", brouillon=True, sorties=tmp_path / "s")
    with TestClient(creer_app(config)) as c:
        assert c.post("/message:send", json=envoi("commande", exemple("commande")), headers=h("seul")).status_code == 400


# FICHE_FOURNISSEUR : le nom du message, partout
def test_le_message_s_appelle_fiche_fournisseur():
    assert "fiche_fournisseur" in NOMS and "fiche_laboratoire" not in NOMS
    assert MEDIA["fiche_fournisseur"] == "application/pharma2a.fiche_fournisseur+json"
    assert (RACINE / "schema" / "fiche_fournisseur.json").exists()
    assert not (RACINE / "schema" / "fiche_laboratoire.json").exists()
    for p in (RACINE / "schema").glob("*.json"):
        assert "laboratoire" not in p.read_text(encoding="utf-8").lower(), p.name
    for p in list((RACINE / "pharma2a").rglob("*.py")) + [RACINE / "README.md"] + list((RACINE / "docs").rglob("*.md")):
        assert "fiche_laboratoire" not in p.read_text(encoding="utf-8"), p.name


# Une officine ne s'approprie pas la tâche d'une autre en recopiant son CIP
# et sa référence ; la répétition ne se reconnaît que parmi les tâches de son propre compte
def test_un_compte_ne_s_approprie_pas_la_tache_d_un_autre_par_le_cip(agent):
    tache_b = agent.post("/message:send", json=envoi("commande", exemple("commande")), headers=h(agent.a)).json()["task"]
    # le jeton B présente son propre compte, mais le CIP et la référence de l'officine A
    doc = copy.deepcopy(exemple("commande"))
    doc["officine"]["numero_compte_client"] = "999999"
    r = agent.post("/message:send", json=envoi("commande", doc), headers=h(agent.b)).json()
    assert r.get("task", {}).get("id") != tache_b["id"]
    assert agent.get(f"/tasks/{tache_b['id']}", headers=h(agent.b)).status_code == 404
    assert agent.post(f"/tasks/{tache_b['id']}:cancel", headers=h(agent.b)).status_code == 404
    t = agent.get(f"/tasks/{tache_b['id']}", headers=h(agent.a)).json()
    assert "compte:999999" not in t["metadata"]["identites"]


# L'auteur relit ses tâches
def test_l_auteur_relit_sa_tache_de_fiche_et_d_etat_de_rayon(agent):
    fiche = agent.post("/message:send", json=envoi("commande", {"version": "1", "demande": "fiche_fournisseur"}), headers=h(agent.a)).json()["task"]
    assert agent.get(f"/tasks/{fiche['id']}", headers=h(agent.a)).status_code == 200
    assert agent.get(f"/tasks/{fiche['id']}", headers=h(agent.b)).status_code == 404
    ray = agent.post("/message:send", json=envoi("etat_de_rayon", exemple("etat_de_rayon")), headers=h(agent.a)).json()["task"]
    assert agent.get(f"/tasks/{ray['id']}", headers=h(agent.a)).status_code == 200
    assert agent.get(f"/tasks/{ray['id']}", headers=h(agent.b)).status_code == 404


def test_une_commande_ne_suit_pas_la_proposition_d_une_autre_officine(agent):
    prop = copy.deepcopy(exemple("commande")); prop["nature"] = "proposition"
    agent.post("/message:send", json=envoi("commande", prop), headers=h(agent.a))
    # l'officine B recopie la référence et le CIP de A, avec son propre compte
    doc = copy.deepcopy(exemple("commande"))
    doc["officine"]["numero_compte_client"] = "999999"
    t = agent.post("/message:send", json=envoi("commande", doc), headers=h(agent.b)).json()["task"]
    texte = t["status"]["message"]["parts"][0]["text"]
    assert "suite de la proposition" not in texte, texte
