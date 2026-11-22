"""Le même renseignement annoncé en une ou deux réponses au bon de commande."""
import json

import pytest

from tests.test_erp import erp  # noqa: F401 (fixture)
from tests.test_relance import envoyer, exemple
from tests.test_relecture import AUTH, agent, relire  # noqa: F401 (fixture)

SIX = {"quantite_confirmee": 4, "reliquat": 2}
CINQ = {"quantite_confirmee": 4, "reliquat": 1}
CAS = [
    ("cinq_direct", [CINQ], "INPUT_REQUIRED", 1),
    ("six_puis_cinq", [SIX, CINQ], "INPUT_REQUIRED", 2),
    ("six_repete", [SIX, SIX], "COMPLETED", 1),
]


def taches_du_contexte(client, racine):
    tasks = client.get("/tasks", params={"contextId": racine["contextId"], "includeArtifacts": "true"},
                       headers=AUTH).json()["tasks"]
    return sorted(tasks, key=lambda t: t["id"] != racine["id"])


@pytest.mark.parametrize("nom,mails,etat_attendu,nb_accuses", CAS)
def test_revisions(agent, nom, mails, etat_attendu, nb_accuses):
    bon = exemple("commande")
    bon["lignes"] = bon["lignes"][:1]
    bon["reliquat_accepte"] = True
    racine = envoyer(agent, "commande", bon)
    boite = agent.sorties / "reponses"
    boite.mkdir(exist_ok=True)
    for n, valeurs in enumerate(mails, 1):
        lignes = [{"id_ligne": "1", "cip13": bon["lignes"][0]["cip13"], **valeurs}]
        accuse = {"version": "1", "reference": bon["reference"], "lignes": lignes}
        texte = f"4 maintenant, {valeurs['reliquat']} en reliquat."
        stem = f"r{n}"
        (boite / (stem + ".eml")).write_text(
            f"From: adv@labo.example\nSubject: RE: {bon['reference']}\n"
            f"Message-ID: <r{n}@labo.example>\n\n" + texte)
        (boite / (stem + ".accuse.json")).write_text(json.dumps(accuse))
        relire(agent)
        assert (boite / "lues" / (stem + ".eml")).is_file()
    tasks = taches_du_contexte(agent, racine)
    assert tasks[-1]["status"]["state"] == "TASK_STATE_" + etat_attendu
    assert sum(1 for t in tasks for a in t["artifacts"] if a["name"] == "accuse_reception") == nb_accuses


def test_reliquat_corrige_par_le_systeme_fournisseur(erp):
    bon = exemple("commande")
    bon.update(reliquat_accepte=True, lignes=bon["lignes"][:1])
    racine = envoyer(erp, "commande", bon)
    envoi = json.loads(next((erp.dossier / "sortie").glob("*.json")).read_text())
    boite = erp.dossier / "entree"
    boite.mkdir()
    for valeurs in (SIX, CINQ):
        accuse = {"version": "1", "reference": bon["reference"], "lignes": [{
            "id_ligne": "1", "cip13": bon["lignes"][0]["cip13"], **valeurs}]}
        (boite / "retour.json").write_text(json.dumps({"en_reponse_a": envoi["message_id"], "accuse": accuse}))
        erp.portal.call(erp.app.state.relire)
    tasks = taches_du_contexte(erp, racine)
    assert len(list((boite / "traites").iterdir())) == 2
    assert tasks[-1]["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert tasks[0]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert tasks[-1]["metadata"]["suite_de"] == racine["id"]
    [accuse] = [a["parts"][0]["data"] for a in tasks[-1]["artifacts"] if a["name"] == "accuse_reception"]
    assert accuse["lignes"][0]["reliquat"] == 1
