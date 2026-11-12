"""L'agent fournisseur d'exemple (examples/sample-supplier) : il tient en une page et parle
vraiment PharmA2A, la pharmacie de démo peut lui commander."""
from __future__ import annotations
import importlib.util
import json
import pathlib
from starlette.testclient import TestClient
from pharma2a.dictionnaire import MEDIA, valider

RACINE = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("sample_supplier", RACINE / "examples" / "sample-supplier" / "agent.py")
sample = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sample)


def envoyer(c, doc):
    corps = {"message": {"messageId": "m-1", "role": "ROLE_USER", "parts": [{"data": doc, "mediaType": MEDIA["commande"]}]}}
    r = c.post("/message:send", json=corps, headers={"A2A-Version": "1.0"})
    assert r.status_code == 200, r.text
    return r.json()["task"]


def test_l_exemple_tient_en_une_page():
    assert len((RACINE / "examples" / "sample-supplier" / "agent.py").read_text(encoding="utf-8").splitlines()) <= 120


def test_la_carte_declare_l_extension():
    with TestClient(sample.app()) as c:
        carte = c.get("/.well-known/agent-card.json").json()
    assert carte["capabilities"]["extensions"][0]["uri"] == "https://pharma2a.org/ext/v1"


def test_une_commande_recoit_un_accuse_conforme_ligne_par_ligne():
    commande = json.loads((RACINE / "schema" / "exemples" / "commande.json").read_text(encoding="utf-8"))
    with TestClient(sample.app()) as c:
        t = envoyer(c, commande)
    a = t["artifacts"][0]["parts"][0]["data"]
    assert valider("accuse_reception", a) == []
    assert [l["id_ligne"] for l in a["lignes"]] == [l["id_ligne"] for l in commande["lignes"]]
    assert t["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"      # un reliquat et une rupture : à trancher


def test_une_commande_hors_dictionnaire_est_refusee():
    with TestClient(sample.app()) as c:
        t = envoyer(c, {"reference": "BC-1", "lignes": [{"cip13": "3400000000011", "quantite": 6, "prix_ht": 9.9}]})
    assert t["status"]["state"] == "TASK_STATE_REJECTED"


def test_une_commande_par_ean_ou_par_reference_fournisseur_recoit_un_accuse_conforme():
    commande = json.loads((RACINE / "schema" / "exemples" / "commande.json").read_text(encoding="utf-8"))
    for l, (k, v) in zip(commande["lignes"], [("ean13", "3400000000011"), ("ref_fournisseur", "REF-28"), ("ean13", "3400000000035")]):
        l.pop("cip13", None); l[k] = v
    assert valider("commande", commande) == []
    with TestClient(sample.app()) as c:
        a = envoyer(c, commande)["artifacts"][0]["parts"][0]["data"]
    assert valider("accuse_reception", a) == []
    assert a["lignes"][1]["ref_fournisseur"] == "REF-28"


def test_une_proposition_n_est_jamais_prise_pour_une_commande():
    commande = json.loads((RACINE / "schema" / "exemples" / "commande.json").read_text(encoding="utf-8"))
    commande["nature"] = "proposition"
    assert valider("commande", commande) == []
    with TestClient(sample.app()) as c:
        t = envoyer(c, commande)
    assert t["status"]["state"] == "TASK_STATE_REJECTED" and not t.get("artifacts")


def test_un_reliquat_refuse_n_est_pas_servi_et_la_quantite_changee_va_au_pharmacien():
    commande = json.loads((RACINE / "schema" / "exemples" / "commande.json").read_text(encoding="utf-8"))
    commande["lignes"] = [{**l, "quantite": 10} for l in commande["lignes"] if l.get("cip13") == "3400000000028"]   # 4 en stock
    assert len(commande["lignes"]) == 1
    for consigne, ligne, attendu in ((None, None, 6), (False, None, 0), (False, True, 6), (True, False, 0)):
        commande.pop("reliquat_accepte", None); commande["lignes"][0].pop("reliquat_accepte", None)
        if consigne is not None: commande["reliquat_accepte"] = consigne
        if ligne is not None: commande["lignes"][0]["reliquat_accepte"] = ligne
        assert valider("commande", commande) == []
        with TestClient(sample.app()) as c:
            t = envoyer(c, commande)
        l = t["artifacts"][0]["parts"][0]["data"]["lignes"][0]
        assert (l["quantite_confirmee"], l["reliquat"]) == (4, attendu), (consigne, ligne)
        assert t["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
