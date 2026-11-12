"""Le canal ERP : la commande validée arrive dans l'ERP telle quelle, l'accusé revient
structuré, ligne par ligne, avec origine « erp », sans qu'aucun modèle le lise."""
import asyncio
import json
import pytest
from starlette.testclient import TestClient
from pharma2a.config import Config
from pharma2a.serveur import creer_app
from pharma2a.backends.erp_exemple import Backend
from tests.test_agent import envoi, exemple, AUTH, JETON


class PasDeModele:
    def extraire(self, *a, **k):
        raise AssertionError("un accusé structuré ne passe pas par le modèle")


@pytest.fixture
def erp(tmp_path, monkeypatch):
    monkeypatch.setenv("PHARMA2A_ERP_SORTIE", str(tmp_path / "sortie"))
    monkeypatch.setenv("PHARMA2A_ERP_ENTREE", str(tmp_path / "entree"))
    config = Config(jeton=JETON, fournisseur="LABO UN", backend="erp_exemple", brouillon=True,
                    sorties=tmp_path / "sorties", relecture_minutes=0)
    with TestClient(creer_app(config, backend=Backend(config), extracteur=PasDeModele())) as c:
        c.dossier = tmp_path
        yield c


def accuse_de(commande, **change):
    a = {"version": "1", "reference": commande["reference"], "origine": "erp",
         "numero_commande_labo": "ERP-778", "date_livraison_prevue": "2026-11-20",
         "lignes": [{"id_ligne": l["id_ligne"], "cip13": l.get("cip13"), "quantite_confirmee": l["quantite"]}
                    for l in commande["lignes"]]}
    a.update(change)
    return a


def test_la_commande_validee_arrive_dans_l_erp_et_l_accuse_revient(erp):
    cmd = exemple("commande")
    t = erp.post("/message:send", json=envoi("commande", cmd), headers=AUTH).json()["task"]
    assert t["status"]["state"] == "TASK_STATE_WORKING"
    depose = json.loads(next((erp.dossier / "sortie").glob("*.json")).read_text())
    assert depose["message"] == "commande" and depose["document"]["reference"] == cmd["reference"]
    assert [l["id_ligne"] for l in depose["document"]["lignes"]] == [l["id_ligne"] for l in cmd["lignes"]]
    assert not list(erp.dossier.rglob("*.eml"))          # aucun mail : l'ERP a reçu le document
    (erp.dossier / "entree").mkdir()
    (erp.dossier / "entree" / "a.json").write_text(json.dumps(accuse_de(cmd)))
    assert asyncio.run(erp.app.state.relire()) == 1
    fin = erp.get(f"/tasks/{t['id']}", headers=AUTH).json()
    assert fin["status"]["state"] == "TASK_STATE_COMPLETED"
    acc = fin["artifacts"][0]["parts"][0]["data"]
    assert acc["origine"] == "erp" and acc["numero_commande_labo"] == "ERP-778"
    assert [f.name.startswith("a-") for f in (erp.dossier / "entree" / "traites").iterdir()] == [True]
    assert not (erp.dossier / "entree" / "a.json").exists()


def test_un_accuse_erp_hors_commande_est_ecarte(erp):
    cmd = exemple("commande")
    t = erp.post("/message:send", json=envoi("commande", cmd), headers=AUTH).json()["task"]
    faux = accuse_de(cmd)
    faux["lignes"][0]["id_ligne"] = "inconnu"
    (erp.dossier / "entree").mkdir()
    (erp.dossier / "entree" / "b.json").write_text(json.dumps(faux))
    assert asyncio.run(erp.app.state.relire()) == 0
    assert erp.get(f"/tasks/{t['id']}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_WORKING"


# --- le canal ERP de référence

def autre_officine(doc):
    import copy
    d = copy.deepcopy(doc)
    d["officine"].update({"nom": "Pharmacie Voisine", "numero_compte_client": "999999", "cip_officine": "2000002"})
    return d


def test_deux_officines_de_meme_reference_ne_s_ecrasent_pas_et_l_accuse_trouve_la_sienne(erp):
    a, b = exemple("commande"), autre_officine(exemple("commande"))
    ta = erp.post("/message:send", json=envoi("commande", a), headers=AUTH).json()["task"]
    tb = erp.post("/message:send", json=envoi("commande", b), headers=AUTH).json()["task"]
    deposes = [json.loads(f.read_text()) for f in (erp.dossier / "sortie").glob("*.json")]
    assert len(deposes) == 2 and {d["document"]["officine"]["nom"] for d in deposes} == {"Pharmacie de l'Exemple", "Pharmacie Voisine"}
    envoi_b = next(d["message_id"] for d in deposes if d["document"]["officine"]["nom"] == "Pharmacie Voisine")
    (erp.dossier / "entree").mkdir()
    # l'accusé seul est ambigu (deux commandes de même référence) : il attend
    (erp.dossier / "entree" / "seul.json").write_text(json.dumps(accuse_de(b)))
    assert asyncio.run(erp.app.state.relire()) == 0
    (erp.dossier / "entree" / "seul.json").unlink()
    # avec l'envoi qu'il cite, il va à la bonne officine
    (erp.dossier / "entree" / "cite.json").write_text(json.dumps({"en_reponse_a": envoi_b, "accuse": accuse_de(b)}))
    assert asyncio.run(erp.app.state.relire()) == 1
    assert erp.get(f"/tasks/{tb['id']}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_COMPLETED"
    assert erp.get(f"/tasks/{ta['id']}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_WORKING"


def test_un_fichier_qui_n_est_pas_un_objet_ne_bloque_pas_les_autres(erp):
    cmd = exemple("commande")
    t = erp.post("/message:send", json=envoi("commande", cmd), headers=AUTH).json()["task"]
    (erp.dossier / "entree").mkdir()
    for nom, contenu in (("0-null.json", "null"), ("1-liste.json", "[]"), ("2-texte.json", '"oui"')):
        (erp.dossier / "entree" / nom).write_text(contenu)
    (erp.dossier / "entree" / "9-bon.json").write_text(json.dumps(accuse_de(cmd)))
    assert asyncio.run(erp.app.state.relire()) == 1
    assert erp.get(f"/tasks/{t['id']}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_COMPLETED"
    assert sorted(f.name for f in (erp.dossier / "entree" / "rejetes").iterdir()) == ["0-null.json", "1-liste.json", "2-texte.json"]


class Minimal:
    """Exactement les trois méthodes documentées, rien d'autre."""
    origine = "erp"

    def __init__(self):
        self.recus = []

    def transmettre(self, message, doc, reference, message_id=None):
        self.recus.append((message, reference))
        return f"ERP-{len(self.recus)}"

    def lire_reponses(self, depuis=None):
        return []

    def acquitter(self, identifiants):
        pass


def test_un_canal_erp_minimal_recoit_commande_etat_de_rayon_et_relance(tmp_path):
    config = Config(jeton=JETON, fournisseur="LABO UN", backend="erp_exemple", brouillon=True,
                    sorties=tmp_path / "sorties", relecture_minutes=0)
    canal = Minimal()
    with TestClient(creer_app(config, backend=canal, extracteur=PasDeModele())) as c:
        assert c.post("/message:send", json=envoi("commande", exemple("commande")), headers=AUTH).json()["task"]["status"]["state"] == "TASK_STATE_WORKING"
        assert c.post("/message:send", json=envoi("etat_de_rayon", exemple("etat_de_rayon")), headers=AUTH).json()["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
        r = c.post("/message:send", json=envoi("relance", exemple("relance")), headers=AUTH).json()["task"]
        assert r["status"]["state"] == "TASK_STATE_WORKING", r["status"]
    assert [m for m, _ in canal.recus] == ["commande", "etat_de_rayon", "relance"]


def test_un_nom_de_fichier_reutilise_pour_un_nouvel_accuse_est_lu(erp):
    """L'ERP dépose retour.json (livraison le 20), puis un nouveau
    retour.json pour le même envoi (le 27). Le second n'est pas une répétition."""
    cmd = exemple("commande")
    t = erp.post("/message:send", json=envoi("commande", cmd), headers=AUTH).json()["task"]
    envoi_id = json.loads(next((erp.dossier / "sortie").glob("*.json")).read_text())["message_id"]
    entree = erp.dossier / "entree"; entree.mkdir()
    for date in ("2026-11-20", "2026-11-27"):
        (entree / "retour.json").write_text(json.dumps({"en_reponse_a": envoi_id, "accuse": accuse_de(cmd, date_livraison_prevue=date)}))
        assert asyncio.run(erp.app.state.relire()) == 1, date
    liste = erp.get("/tasks", params={"contextId": t["contextId"]}, headers=AUTH).json()["tasks"]
    [s] = [x for x in liste if (x.get("metadata") or {}).get("suite_de") == t["id"]]
    assert "livraison décalée au 2026-11-27" in s["status"]["message"]["parts"][0]["text"]
    assert len(list((entree / "traites").iterdir())) == 2


def test_le_pas_a_pas_erp_du_readme(erp):
    """Le parcours « Try it with an ERP in 10 minutes » : la commande d'exemple arrive dans
    l'ERP, l'accusé d'exemple revient avec l'envoi qu'il cite, la tâche passe à trancher."""
    t = erp.post("/message:send", json=envoi("commande", exemple("commande")), headers=AUTH).json()["task"]
    assert t["status"]["state"] == "TASK_STATE_WORKING"
    envoi_erp = json.loads(next((erp.dossier / "sortie").glob("commande-*.json")).read_text())
    assert set(envoi_erp) == {"message", "message_id", "reference", "document"}
    (erp.dossier / "entree").mkdir()
    (erp.dossier / "entree" / "retour.json").write_text(json.dumps(
        {"en_reponse_a": envoi_erp["message_id"], "accuse": exemple("accuse_reception")}))
    assert asyncio.run(erp.app.state.relire()) == 1
    fin = erp.get(f"/tasks/{t['id']}", headers=AUTH).json()
    assert fin["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    acc = next(a for a in fin["artifacts"] if a["name"] == "accuse_reception")["parts"][0]["data"]
    assert acc["numero_commande_labo"] == "SO-884512"
    assert [f.name.startswith("retour-") for f in (erp.dossier / "entree" / "traites").iterdir()] == [True]


def test_la_pharmacie_de_demonstration(erp, monkeypatch, capsys):
    """python -m pharma2a.officine : la carte, la commande d'exemple sous une référence neuve,
    puis la tâche relue avec l'accusé, les quantités montrées entières."""
    from pharma2a import officine

    def appel(self, methode, chemin, corps=None, **_):
        r = erp.request(methode, chemin, json=corps, headers=dict(AUTH, **{"A2A-Extensions": officine.EXTENSION}))
        assert r.status_code == 200, r.text
        return r.json()
    monkeypatch.setattr(officine.Agent, "appel", appel)
    assert officine.main(["--jeton", JETON]) == 0
    sortie = capsys.readouterr().out
    # la démo demande une réponse immédiate (returnImmediately) : la tâche est rendue soumise ou en cours
    assert "extension PharmA2A déclarée" in sortie and ("TASK_STATE_WORKING" in sortie or "TASK_STATE_SUBMITTED" in sortie)
    envoi_erp = json.loads(next((erp.dossier / "sortie").glob("commande-*.json")).read_text())
    assert envoi_erp["reference"].startswith("DEMO-")
    accuse = exemple("accuse_reception"); accuse["reference"] = envoi_erp["reference"]
    (erp.dossier / "entree").mkdir()
    (erp.dossier / "entree" / "retour.json").write_text(json.dumps({"en_reponse_a": envoi_erp["message_id"], "accuse": accuse}))
    assert asyncio.run(erp.app.state.relire()) == 1
    tache = sortie.split("tâche ")[1].split(" ")[0]
    assert officine.main(["--tache", tache]) == 0
    sortie = capsys.readouterr().out
    assert "TASK_STATE_INPUT_REQUIRED" in sortie and "SO-884512" in sortie
    assert "12 confirmé(s), 9 en reliquat" in sortie and "rupture" in sortie


def test_la_pharmacie_de_demonstration_n_envoie_pas_une_commande_hors_dictionnaire(erp, tmp_path, monkeypatch):
    from pharma2a import officine
    monkeypatch.setattr(officine.Agent, "appel", lambda self, m, c, corps=None, **_: (
        {"name": "x"} if c.endswith("agent-card.json") else pytest.fail("rien ne doit partir")))
    f = tmp_path / "mauvaise.json"
    cmd = exemple("commande"); cmd["lignes"][0]["prix_ht"] = 9.9
    f.write_text(json.dumps(cmd))
    assert officine.main(["--commande", str(f)]) == 2
    assert not (erp.dossier / "sortie").exists()


def test_un_accuse_remplace_pendant_l_acquittement_reste_a_lire(tmp_path, monkeypatch):
    """L'ERP remplace retour.json entre la lecture et l'acquittement : la nouvelle réponse n'est
    pas archivée sous l'empreinte de l'ancienne, elle reste à lire à la passe suivante."""
    from pharma2a.backends import erp_exemple
    monkeypatch.setenv("PHARMA2A_ERP_ENTREE", str(tmp_path / "entree"))
    canal = erp_exemple.Backend(Config(jeton=JETON))
    canal.entree.mkdir()
    (canal.entree / "retour.json").write_text(json.dumps({"reference": "BC-1", "lignes": []}))
    [lu] = canal.lire_reponses()
    # l'ERP écrit le nouvel accusé à l'instant où l'agent déplace le fichier
    import pathlib
    vrai_replace, fait = pathlib.Path.replace, []
    def replace_avec_course(self, cible):
        if self.name == "retour.json" and not fait:
            fait.append(1)
            self.write_text(json.dumps({"reference": "BC-1", "lignes": [], "date_livraison_prevue": "2026-11-27"}))
        return vrai_replace(self, cible)
    monkeypatch.setattr(pathlib.Path, "replace", replace_avec_course)
    canal.acquitter([lu.identifiant])
    monkeypatch.setattr(pathlib.Path, "replace", vrai_replace)
    [encore] = canal.lire_reponses()
    assert encore.accuse["date_livraison_prevue"] == "2026-11-27"
    assert not list((canal.entree / "traites").iterdir())
    canal.acquitter([encore.identifiant])
    assert canal.lire_reponses() == [] and len(list((canal.entree / "traites").iterdir())) == 1


def test_un_nom_de_fichier_long_n_empeche_pas_l_archivage(tmp_path, monkeypatch):
    """Un nom de 240 octets plus l'empreinte dépasserait 255 octets : l'archive est bornée, et le
    fichier suivant est archivé lui aussi."""
    from pharma2a.backends import erp_exemple
    monkeypatch.setenv("PHARMA2A_ERP_ENTREE", str(tmp_path / "entree"))
    canal = erp_exemple.Backend(Config(jeton=JETON))
    canal.entree.mkdir()
    (canal.entree / ("a" * 235 + ".json")).write_text(json.dumps({"reference": "BC-1", "lignes": []}))
    (canal.entree / "b.json").write_text(json.dumps({"reference": "BC-2", "lignes": []}))
    canal.acquitter([r.identifiant for r in canal.lire_reponses()])
    archives = list((canal.entree / "traites").iterdir())
    assert len(archives) == 2 and all(len(a.name.encode()) <= 255 for a in archives)
    assert canal.lire_reponses() == []


def test_un_accuse_garde_en_reserve_passe_avant_celui_qui_lui_succede(tmp_path, monkeypatch):
    """B remplace A pendant l'acquittement et reste en réserve ; puis l'ERP dépose C sous le même
    nom. Rien n'est écrasé, et B, déposé avant C, se lit avant lui : l'ancienne date (27/11) ne
    revient jamais après la nouvelle (04/12)."""
    import pathlib
    import time
    from pharma2a.backends import erp_exemple
    monkeypatch.setenv("PHARMA2A_ERP_ENTREE", str(tmp_path / "entree"))
    canal = erp_exemple.Backend(Config(jeton=JETON))
    canal.entree.mkdir()
    f = canal.entree / "retour.json"
    doc = lambda date: json.dumps({"reference": "BC-1", "lignes": [], "date_livraison_prevue": date})
    f.write_text(doc("2026-11-20"))
    [lu] = canal.lire_reponses()
    vrai_replace = pathlib.Path.replace
    def replace_course(self, cible):
        if self.name == "retour.json" and self.parent == canal.entree:
            self.write_text(doc("2026-11-27"))          # B, à l'instant de la réservation
        return vrai_replace(self, cible)
    monkeypatch.setattr(pathlib.Path, "replace", replace_course)
    canal.acquitter([lu.identifiant])
    monkeypatch.setattr(pathlib.Path, "replace", vrai_replace)
    time.sleep(0.01)
    f.write_text(doc("2026-12-04"))                      # C, déposé ensuite
    dates = [r.accuse["date_livraison_prevue"] for r in canal.lire_reponses()]
    assert dates == ["2026-11-27", "2026-12-04"]
    canal.acquitter([r.identifiant for r in canal.lire_reponses()])
    assert canal.lire_reponses() == [] and len(list((canal.entree / "traites").iterdir())) == 2


def test_un_fichier_remplace_par_un_accuse_valide_n_est_pas_rejete(tmp_path, monkeypatch):
    """L'ERP remplace un « [] » par un accusé valide entre la lecture et le rejet : c'est la
    version réservée qui est jugée, l'accusé valide reste à lire."""
    import pathlib
    from pharma2a.backends import erp_exemple
    monkeypatch.setenv("PHARMA2A_ERP_ENTREE", str(tmp_path / "entree"))
    canal = erp_exemple.Backend(Config(jeton=JETON))
    canal.entree.mkdir()
    f = canal.entree / "retour.json"
    f.write_text("[]")
    vrai_replace = pathlib.Path.replace
    def replace_course(self, cible):
        if self.name == "retour.json" and self.parent == canal.entree:
            self.write_text(json.dumps({"reference": "BC-1", "lignes": []}))
        return vrai_replace(self, cible)
    monkeypatch.setattr(pathlib.Path, "replace", replace_course)
    assert canal.lire_reponses() == []
    monkeypatch.setattr(pathlib.Path, "replace", vrai_replace)
    assert not (canal.entree / "rejetes").exists() or not list((canal.entree / "rejetes").iterdir())
    [r] = canal.lire_reponses()
    assert r.accuse["reference"] == "BC-1"


def test_une_reservation_interrompue_est_reprise(tmp_path, monkeypatch):
    from pharma2a.backends import erp_exemple
    monkeypatch.setenv("PHARMA2A_ERP_ENTREE", str(tmp_path / "entree"))
    canal = erp_exemple.Backend(Config(jeton=JETON))
    case = canal.entree / ".reserve" / "abc"
    case.mkdir(parents=True)
    (case / "retour.json").write_text(json.dumps({"reference": "BC-1", "lignes": []}))
    [r] = canal.lire_reponses()
    assert r.identifiant.startswith("retour.json#")
    canal.acquitter([r.identifiant])
    assert not case.exists() and len(list((canal.entree / "traites").iterdir())) == 1


def test_a_horodatage_egal_la_reserve_passe_avant_et_un_fichier_illisible_attend(tmp_path, monkeypatch):
    """Deux accusés au même horodatage : celui de la réserve, déposé avant, se lit d'abord. Un
    fichier remplacé pendant son rejet par une version encore en cours d'écriture n'est pas
    rejeté : il attend."""
    import os
    import pathlib
    from pharma2a.backends import erp_exemple
    monkeypatch.setenv("PHARMA2A_ERP_ENTREE", str(tmp_path / "entree"))
    canal = erp_exemple.Backend(Config(jeton=JETON))
    case = canal.entree / ".reserve" / "a"
    case.mkdir(parents=True)
    ancien, nouveau = case / "z.json", canal.entree / "a.json"
    ancien.write_text(json.dumps({"reference": "BC-1", "lignes": [], "date_livraison_prevue": "2026-11-27"}))
    nouveau.write_text(json.dumps({"reference": "BC-1", "lignes": [], "date_livraison_prevue": "2026-12-04"}))
    os.utime(ancien, ns=(10**18, 10**18)); os.utime(nouveau, ns=(10**18, 10**18))
    assert [r.accuse["date_livraison_prevue"] for r in canal.lire_reponses()] == ["2026-11-27", "2026-12-04"]
    vide = canal.entree / "v.json"
    vide.write_text("[]")
    vrai_replace = pathlib.Path.replace
    def replace_course(self, cible):
        if self.name == "v.json" and self.parent == canal.entree:
            self.write_text('{"reference": "BC-2", "lig')      # l'ERP écrit encore
        return vrai_replace(self, cible)
    monkeypatch.setattr(pathlib.Path, "replace", replace_course)
    canal.lire_reponses()
    monkeypatch.setattr(pathlib.Path, "replace", vrai_replace)
    assert not (canal.entree / "rejetes").exists() or not list((canal.entree / "rejetes").iterdir())
