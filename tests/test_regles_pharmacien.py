"""Les règles que les pharmaciens ont données (un formulaire rempli par des pharmaciens) : la date de
livraison ferme, le reliquat accepté ou non, l'accord sur l'état de rayon, et le second mail
du fournisseur qui ne se perd plus."""
import json
import uuid
import pytest
from datetime import datetime, timedelta, timezone
from google.protobuf.json_format import MessageToDict
from a2a.helpers import new_data_message
from a2a.types import SendMessageRequest, Role
from pharma2a import courrier
from pharma2a.dictionnaire import MEDIA, valider
from pharma2a.relecture import a_l_ecoute, ecart
from tests.test_relecture import agent, relire, EML, EXEMPLES, AUTH   # noqa: F401 (fixture)


def commande(**champs):
    doc = json.loads((EXEMPLES / "commande.json").read_text(encoding="utf-8"))
    doc.update(champs)
    return doc


def envoyer(c, doc):
    m = new_data_message(doc, media_type=MEDIA["commande"], role=Role.ROLE_USER); m.message_id = str(uuid.uuid4())
    r = c.post("/message:send", json=MessageToDict(SendMessageRequest(message=m)), headers=AUTH).json()
    assert r["task"]["status"]["state"] == "TASK_STATE_WORKING", r
    return r["task"]["id"]


def repondre(c, nom, accuse):
    d = c.sorties / "reponses"; d.mkdir(parents=True, exist_ok=True)
    (d / f"{nom}.eml").write_bytes(EML)
    (d / f"{nom}.accuse.json").write_text(json.dumps(accuse, ensure_ascii=False), encoding="utf-8")


def accuse(**champs):
    a = {"version": "1", "reference": "BC-26018-EXEMPLE",
         "lignes": [{"id_ligne": "1", "cip13": "3400000000011", "quantite_confirmee": 6},
                    {"id_ligne": "2", "cip13": "3400000000028", "quantite_confirmee": 21},
                    {"id_ligne": "3", "cip13": "3400000000035", "quantite_confirmee": 4}]}
    a.update(champs)
    return a


def tache(c, tid):
    return c.get(f"/tasks/{tid}", headers=AUTH).json()


# --- le dictionnaire

def test_les_nouveaux_champs_sont_facultatifs_et_valides():
    assert valider("commande", commande()) == []
    assert valider("commande", commande(date_livraison_exigee=True, reliquat_accepte=False)) == []
    doc = commande(); doc["lignes"][0]["reliquat_accepte"] = True
    assert valider("commande", doc) == []
    assert valider("commande", commande(reliquat_accepte="oui"))
    ray = json.loads((EXEMPLES / "etat_de_rayon.json").read_text(encoding="utf-8"))
    assert valider("etat_de_rayon", ray) == []
    ray["accord"] = {"usage": "livraison", "valable_jusqu_au": None, "frequence": "commande"}
    assert valider("etat_de_rayon", ray) == []
    ray["accord"] = {"usage": "revente"}
    assert valider("etat_de_rayon", ray)
    ray["accord"] = {"frequence": "mensuelle"}
    assert valider("etat_de_rayon", ray)


# --- la date ferme

def test_sans_date_ferme_exigee_la_commande_revient_au_pharmacien(agent):
    tid = envoyer(agent, commande(date_livraison_exigee=True))
    repondre(agent, "r1", accuse(numero_commande_labo="SO-1"))
    assert relire(agent) == 1
    t = tache(agent, tid)
    assert t["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert "date de livraison ferme" in t["status"]["message"]["parts"][0]["text"]


def test_avec_la_date_ferme_la_commande_est_confirmee(agent):
    tid = envoyer(agent, commande(date_livraison_exigee=True))
    repondre(agent, "r1", accuse(numero_commande_labo="SO-1", date_livraison_prevue="2026-11-20"))
    assert relire(agent) == 1
    assert tache(agent, tid)["status"]["state"] == "TASK_STATE_COMPLETED"


def test_sans_exigence_un_numero_suffit_comme_avant(agent):
    tid = envoyer(agent, commande())
    repondre(agent, "r1", accuse(numero_commande_labo="SO-1"))
    relire(agent)
    assert tache(agent, tid)["status"]["state"] == "TASK_STATE_COMPLETED"


# --- le reliquat

def reliquat_sur_la_1():
    a = accuse(numero_commande_labo="SO-1", date_livraison_prevue="2026-11-20")
    a["lignes"][0].update({"quantite_confirmee": 4, "reliquat": 2})
    return a


def test_un_reliquat_accepte_d_avance_ne_demande_rien(agent):
    tid = envoyer(agent, commande(reliquat_accepte=True))
    repondre(agent, "r1", reliquat_sur_la_1())
    relire(agent)
    t = tache(agent, tid)
    assert t["status"]["state"] == "TASK_STATE_COMPLETED"
    assert "accepté d'avance" in t["status"]["message"]["parts"][0]["text"]


def test_la_ligne_prime_sur_la_commande_pour_le_reliquat(agent):
    doc = commande(reliquat_accepte=True); doc["lignes"][0]["reliquat_accepte"] = False
    tid = envoyer(agent, doc)
    repondre(agent, "r1", reliquat_sur_la_1())
    relire(agent)
    assert tache(agent, tid)["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"


def test_sans_rien_dire_le_reliquat_se_tranche_comme_avant(agent):
    tid = envoyer(agent, commande())
    repondre(agent, "r1", reliquat_sur_la_1())
    relire(agent)
    assert tache(agent, tid)["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"


# --- le mail dit ce que l'officine attend

def test_le_bon_dit_la_date_ferme_et_le_reliquat():
    doc = commande(date_livraison_exigee=True, reliquat_accepte=False); doc["lignes"][1]["reliquat_accepte"] = True
    _, texte, html = courrier.commande(doc)
    for t in (texte, html):
        assert "date de livraison ferme" in t and "Pas de reliquat" in t and "(reliquat accepté)" in t
    _, texte, _ = courrier.commande(commande())
    assert "reliquat" not in texte.lower() and "ferme" not in texte


def test_l_etat_de_rayon_dit_l_accord_du_pharmacien():
    ray = json.loads((EXEMPLES / "etat_de_rayon.json").read_text(encoding="utf-8"))
    ray["accord"] = {"usage": "livraison", "valable_jusqu_au": None, "frequence": "commande"}
    _, texte, html, _ = courrier.etat_de_rayon(ray)
    assert "uniquement à mieux livrer" in texte and "jusqu'à ce que le pharmacien le retire" in texte
    ray["accord"] = {"usage": "previsions", "valable_jusqu_au": "2027-05-01"}
    _, texte, _, _ = courrier.etat_de_rayon(ray)
    assert "prévisions" in texte and "jusqu'au 01/05/2027" in texte


# --- le second mail du fournisseur

def suites(c, t):
    """Les tâches de suite d'une tâche, lues dans son contexte."""
    r = c.get("/tasks", params={"contextId": t["contextId"], "includeArtifacts": "true"}, headers=AUTH).json()
    return [x for x in r.get("tasks") or [] if (x.get("metadata") or {}).get("suite_de") == t["id"]]


def test_un_second_mail_qui_change_la_livraison_revient_au_pharmacien(agent):
    tid = envoyer(agent, commande())
    repondre(agent, "r1", accuse(numero_commande_labo="SO-1", date_livraison_prevue="2026-11-20"))
    relire(agent)
    premiere = tache(agent, tid)
    assert premiere["status"]["state"] == "TASK_STATE_COMPLETED"
    repondre(agent, "r2", {"version": "1", "reference": "BC-26018-EXEMPLE", "date_livraison_prevue": "2026-11-27", "lignes": []})
    assert relire(agent) == 1
    # Une tâche terminée ne redémarre pas (A2A) : elle reste telle quelle
    assert tache(agent, tid) == premiere
    [s] = suites(agent, premiere)
    assert s["contextId"] == premiere["contextId"] and s["id"] != tid
    assert s["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert "livraison décalée au 2026-11-27" in s["status"]["message"]["parts"][0]["text"]
    assert s["history"][0]["referenceTaskIds"] == [tid]
    assert s["metadata"]["reference"] == "BC-26018-EXEMPLE" and s["metadata"]["identites"] == premiere["metadata"]["identites"]
    accuses = [a for a in s["artifacts"] if a["name"] == "accuse_reception"]
    assert len(accuses) == 1 and accuses[0]["parts"][0]["data"]["date_livraison_prevue"] == "2026-11-27"


def test_un_second_mail_qui_complete_garde_l_etat(agent):
    tid = envoyer(agent, commande())
    repondre(agent, "r1", accuse(date_livraison_prevue="2026-11-20"))
    relire(agent)
    premiere = tache(agent, tid)
    assert premiere["status"]["state"] == "TASK_STATE_COMPLETED"
    repondre(agent, "r2", {"version": "1", "reference": "BC-26018-EXEMPLE", "numero_commande_labo": "SO-9", "lignes": []})
    assert relire(agent) == 1
    assert tache(agent, tid) == premiere
    [s] = suites(agent, premiere)
    assert s["status"]["state"] == "TASK_STATE_COMPLETED"
    assert "Complément du fournisseur" in s["status"]["message"]["parts"][0]["text"]


def test_le_troisieme_mail_va_a_la_suite_et_la_commande_repetee_rend_la_premiere(agent):
    doc = commande()
    tid = envoyer(agent, doc)
    repondre(agent, "r1", accuse(numero_commande_labo="SO-1", date_livraison_prevue="2026-11-20")); relire(agent)
    premiere = tache(agent, tid)
    repondre(agent, "r2", {"version": "1", "reference": "BC-26018-EXEMPLE", "date_livraison_prevue": "2026-11-27", "lignes": []})
    assert relire(agent) == 1
    [s] = suites(agent, premiere)
    # le mail suivant lit la suite, pas la première : pas d'ambiguïté, pas de seconde suite
    repondre(agent, "r3", {"version": "1", "reference": "BC-26018-EXEMPLE", "date_livraison_prevue": "2026-12-04", "lignes": []})
    assert relire(agent) == 1
    assert len(suites(agent, premiere)) == 1
    s2 = tache(agent, s["id"])
    assert "livraison décalée au 2026-12-04" in s2["status"]["message"]["parts"][0]["text"]
    # la même commande renvoyée (coupure réseau) rend la première tâche, jamais la suite
    m = new_data_message(doc, media_type=MEDIA["commande"], role=Role.ROLE_USER); m.message_id = str(uuid.uuid4())
    r = agent.post("/message:send", json=MessageToDict(SendMessageRequest(message=m)), headers=AUTH).json()
    assert r["task"]["id"] == tid


def test_un_second_mail_qui_ne_dit_rien_de_neuf_ne_touche_a_rien(agent):
    tid = envoyer(agent, commande())
    a = accuse(numero_commande_labo="SO-1", date_livraison_prevue="2026-11-20")
    repondre(agent, "r1", a); relire(agent)
    repondre(agent, "r2", a)
    assert relire(agent) == 0
    t = tache(agent, tid)
    assert len([x for x in t["artifacts"] if x["name"] == "accuse_reception"]) == 1
    assert (agent.sorties / "reponses" / "lues" / "r2.eml").exists()


def test_une_tache_fermee_par_l_officine_n_ecoute_plus(agent):
    tid = envoyer(agent, commande())
    repondre(agent, "r1", reliquat_sur_la_1()); relire(agent)
    assert tache(agent, tid)["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert agent.post(f"/tasks/{tid}:cancel", headers=AUTH).status_code == 200
    repondre(agent, "r2", {"version": "1", "reference": "BC-26018-EXEMPLE", "date_livraison_prevue": "2026-11-27", "lignes": []})
    assert relire(agent) == 0
    assert tache(agent, tid)["status"]["state"] == "TASK_STATE_CANCELED"


def test_l_ecoute_dure_trente_jours():
    from a2a.types import Task, TaskStatus, TaskState
    t = Task(id="t", context_id="c", status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED))
    t.metadata.update({"message_id": "<m@x>"})
    il_y_a = datetime.now(timezone.utc) - timedelta(days=10)
    t.status.timestamp.FromDatetime(il_y_a)
    assert a_l_ecoute(t)
    assert not a_l_ecoute(t, maintenant=il_y_a + timedelta(days=31))
    t.status.state = TaskState.TASK_STATE_CANCELED
    assert not a_l_ecoute(t)


def test_l_ecart_ignore_une_ligne_dont_le_mail_ne_parle_pas():
    cmd = commande()
    avant = accuse(numero_commande_labo="SO-1")
    change, complete = ecart(avant, {"version": "1", "reference": "BC-26018-EXEMPLE", "lignes": []}, cmd)
    assert change == [] and complete == []
    apres = {"lignes": [{"id_ligne": "2", "quantite_confirmee": 12}]}
    change, _ = ecart(avant, apres, cmd)
    assert change and "12 au lieu de 21" in change[0]


def test_le_troisieme_mail_se_compare_a_tout_ce_qui_est_connu(agent):
    """La date du premier mail, le numéro seul dans le second, une autre date
    dans le troisième. Le troisième décale la livraison, ce n'est pas un complément."""
    tid = envoyer(agent, commande())
    repondre(agent, "r1", accuse(date_livraison_prevue="2026-11-20")); relire(agent)
    premiere = tache(agent, tid)
    repondre(agent, "r2", {"version": "1", "reference": "BC-26018-EXEMPLE", "numero_commande_labo": "SO-9", "lignes": []})
    assert relire(agent) == 1
    [s] = suites(agent, premiere)
    assert s["status"]["state"] == "TASK_STATE_COMPLETED"
    repondre(agent, "r3", {"version": "1", "reference": "BC-26018-EXEMPLE", "date_livraison_prevue": "2026-11-27", "lignes": []})
    assert relire(agent) == 1
    s = tache(agent, s["id"])
    [s2] = suites(agent, s)
    assert s2["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert "livraison décalée au 2026-11-27" in s2["status"]["message"]["parts"][0]["text"]


def test_une_reponse_dont_l_acquittement_a_echoue_ne_cree_pas_une_seconde_suite(agent, monkeypatch):
    from pharma2a.backends import mail
    tid = envoyer(agent, commande())
    repondre(agent, "r1", accuse(numero_commande_labo="SO-1", date_livraison_prevue="2026-11-20")); relire(agent)
    premiere = tache(agent, tid)
    # un complément avec une pièce jointe : sans mémoire de la réponse, chaque passe l'aurait reprise
    from tests.test_relecture import EML_AVEC_PIECE
    d = agent.sorties / "reponses"
    (d / "r2.eml").write_bytes(EML_AVEC_PIECE)
    (d / "r2.accuse.json").write_text(json.dumps({"version": "1", "reference": "BC-26018-EXEMPLE", "numero_commande_labo": "SO-1", "lignes": []}), encoding="utf-8")
    vrai = mail.Backend.acquitter
    monkeypatch.setattr(mail.Backend, "acquitter", lambda self, ids: (_ for _ in ()).throw(OSError("boîte injoignable")))
    assert relire(agent) == 1
    monkeypatch.setattr(mail.Backend, "acquitter", vrai)
    # la même réponse revient : reconnue, acquittée, rien de neuf
    assert relire(agent) == 0
    assert len(suites(agent, premiere)) == 1
    assert (agent.sorties / "reponses" / "lues" / "r2.eml").exists()


# --- les décisions métier

def test_une_consigne_changee_n_est_pas_une_repetition(agent):
    tid = envoyer(agent, commande())
    m = new_data_message(commande(date_livraison_exigee=True), media_type=MEDIA["commande"], role=Role.ROLE_USER); m.message_id = str(uuid.uuid4())
    r = agent.post("/message:send", json=MessageToDict(SendMessageRequest(message=m)), headers=AUTH).json()["task"]
    assert r["id"] != tid and r["status"]["state"] == "TASK_STATE_REJECTED"


def test_un_etat_de_rayon_d_une_autre_officine_ou_d_un_autre_fournisseur_est_refuse(agent):
    for change in ({"officine": {"nom": "Autre", "numero_compte_client": "999", "cip_officine": "2999999"}}, {"fournisseur": "AUTRE LABO"}):
        ray = json.loads((EXEMPLES / "etat_de_rayon.json").read_text(encoding="utf-8")) | change
        doc = commande(reference="BC-R-" + list(change)[0].upper(), etat_de_rayon=ray)
        m = new_data_message(doc, media_type=MEDIA["commande"], role=Role.ROLE_USER); m.message_id = str(uuid.uuid4())
        t = agent.post("/message:send", json=MessageToDict(SendMessageRequest(message=m)), headers=AUTH).json()["task"]
        assert t["status"]["state"] == "TASK_STATE_REJECTED", change
        assert "etat_de_rayon/" + list(change)[0] in str(t["status"]["message"])


def test_une_repetition_avec_le_rayon_d_un_autre_fournisseur_est_refusee(agent):
    """La même commande renvoyée avec un état de rayon au nom d'un autre fournisseur : le guichet
    ne rend pas la tâche existante comme un succès, l'exécuteur refuse, rien ne repart."""
    ray = json.loads((EXEMPLES / "etat_de_rayon.json").read_text(encoding="utf-8"))
    tid = envoyer(agent, commande(reference="BC-R-REPET", etat_de_rayon=ray))
    mails = sorted(agent.sorties.glob("*.eml"))
    doc = commande(reference="BC-R-REPET", etat_de_rayon=ray | {"fournisseur": "AUTRE LABO"})
    m = new_data_message(doc, media_type=MEDIA["commande"], role=Role.ROLE_USER); m.message_id = str(uuid.uuid4())
    t = agent.post("/message:send", json=MessageToDict(SendMessageRequest(message=m)), headers=AUTH).json()["task"]
    assert t["id"] != tid and t["status"]["state"] == "TASK_STATE_REJECTED"
    assert "etat_de_rayon/fournisseur" in str(t["status"]["message"])
    assert sorted(agent.sorties.glob("*.eml")) == mails


def test_une_suite_fermee_ferme_tout_le_fil(agent):
    tid = envoyer(agent, commande())
    repondre(agent, "r1", accuse(numero_commande_labo="SO-1", date_livraison_prevue="2026-11-20")); relire(agent)
    premiere = tache(agent, tid)
    repondre(agent, "r2", {"version": "1", "reference": "BC-26018-EXEMPLE", "date_livraison_prevue": "2026-11-27", "lignes": []})
    relire(agent)
    [s] = suites(agent, premiere)
    assert agent.post(f"/tasks/{s['id']}:cancel", headers=AUTH).status_code == 200
    repondre(agent, "r3", {"version": "1", "reference": "BC-26018-EXEMPLE", "date_livraison_prevue": "2026-12-04", "lignes": []})
    assert relire(agent) == 0
    assert len(suites(agent, premiere)) == 1 and tache(agent, tid) == premiere


@pytest.mark.parametrize("sqlite", [False, True])
def test_annuler_une_commande_repondue_garde_l_accuse(tmp_path, sqlite):
    """L'exécution gardait en mémoire la tâche « working » ;
    annuler après la réponse du fournisseur réécrivait cette copie et effaçait l'accusé."""
    from starlette.testclient import TestClient
    from pharma2a.config import Config
    from pharma2a.serveur import creer_app
    from tests.test_relecture import JETON, ExtracteurFichier
    d = tmp_path / "courrier"
    config = Config(jeton=JETON, fournisseur="LABO UN", destinataire="adv@labo.example", brouillon=True, sorties=d,
                    base=str(tmp_path / "taches.sqlite") if sqlite else "", relecture_minutes=0)
    app = creer_app(config, extracteur=ExtracteurFichier(d / "reponses"))
    with TestClient(app) as c:
        c.app_, c.sorties = app, d
        tid = envoyer(c, commande())
        repondre(c, "r1", reliquat_sur_la_1())
        assert relire(c) == 1
        avant = tache(c, tid)
        assert avant["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
        assert c.post(f"/tasks/{tid}:cancel", headers=AUTH).status_code == 200
        apres = tache(c, tid)
        assert apres["status"]["state"] == "TASK_STATE_CANCELED"
        assert apres["artifacts"] == avant["artifacts"]
        assert apres["metadata"]["reponses_lues"] == avant["metadata"]["reponses_lues"] == ["r1.eml"]


# --- les prix du fournisseur

def test_l_accuse_porte_les_prix_du_fournisseur_et_un_prix_net_change_revient_au_pharmacien(agent):
    tid = envoyer(agent, commande())
    a = accuse(numero_commande_labo="SO-1", date_livraison_prevue="2026-11-20")
    for l in a["lignes"]:
        l.update(prix_tarif_ht=10.0, remise_pct=50, prix_net_ht=5.0, taux_tva=5.5)
    repondre(agent, "r1", a); relire(agent)
    premiere = tache(agent, tid)
    assert premiere["status"]["state"] == "TASK_STATE_COMPLETED"
    acc = premiere["artifacts"][0]["parts"][0]["data"]
    assert acc["lignes"][0]["remise_pct"] == 50 and acc["lignes"][0]["prix_net_ht"] == 5 and acc["lignes"][0]["taux_tva"] == 5.5
    repondre(agent, "r2", {"version": "1", "reference": "BC-26018-EXEMPLE",
                           "lignes": [{"id_ligne": "1", "cip13": "3400000000011", "prix_net_ht": 6.5}]})
    assert relire(agent) == 1
    [s] = suites(agent, premiere)
    assert s["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert "prix net 6,50 € HT au lieu de 5,00 €" in s["status"]["message"]["parts"][0]["text"]



def test_une_remise_pour_toute_la_commande_se_dit_une_fois(agent):
    tid = envoyer(agent, commande())
    repondre(agent, "r1", accuse(numero_commande_labo="SO-1", date_livraison_prevue="2026-11-20", remise_pct=50)); relire(agent)
    premiere = tache(agent, tid)
    assert premiere["status"]["state"] == "TASK_STATE_COMPLETED"
    assert premiere["artifacts"][0]["parts"][0]["data"]["remise_pct"] == 50
    repondre(agent, "r2", {"version": "1", "reference": "BC-26018-EXEMPLE", "remise_pct": 40, "lignes": []})
    assert relire(agent) == 1
    [s] = suites(agent, premiere)
    assert s["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert "remise de la commande 40 % au lieu de 50 %" in s["status"]["message"]["parts"][0]["text"]


def test_une_confirmation_explicite_ou_un_tarif_apres_une_premiere_reponse_sont_gardes(agent):
    """Une première réponse ne donne que la remise : à trancher. Le fournisseur confirme ensuite
    explicitement, sans numéro ni date, puis donne le tarif et la TVA d'une ligne : chacun de ces
    compléments est un nouvel accusé, rien n'est acquitté sans être enregistré."""
    tid = envoyer(agent, commande())
    repondre(agent, "r1", {"version": "1", "reference": "BC-26018-EXEMPLE", "remise_pct": 10, "lignes": []}); relire(agent)
    assert tache(agent, tid)["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    repondre(agent, "r2", {"version": "1", "reference": "BC-26018-EXEMPLE", "confirmation_globale": True, "lignes": []})
    assert relire(agent) == 1
    t = tache(agent, tid)
    assert "le fournisseur confirme la commande" in t["status"]["message"]["parts"][0]["text"]
    repondre(agent, "r3", {"version": "1", "reference": "BC-26018-EXEMPLE",
                           "lignes": [{"id_ligne": "1", "cip13": "3400000000011", "prix_tarif_ht": 9.9, "remise_pct": 20, "taux_tva": 5.5}]})
    assert relire(agent) == 1
    t = tache(agent, tid)
    texte = t["status"]["message"]["parts"][0]["text"]
    assert "tarif 9,90 € HT" in texte and "remise 20 %" in texte and "TVA 5.5 %" in texte
    accuses = [a["parts"][0]["data"] for a in t["artifacts"] if a["name"] == "accuse_reception"]
    assert len(accuses) == 3 and accuses[1]["confirmation_globale"] is True


def test_un_ancien_mail_ne_fait_pas_revenir_une_date_corrigee(agent, monkeypatch):
    """Sur une tâche à trancher, un mail répète la date connue et son acquittement échoue ; un
    nouveau mail décale la livraison, son acquittement échoue aussi. Quand les deux reviennent,
    l'ancien n'est pas pris pour un nouveau décalage : la date reste la dernière annoncée."""
    from pharma2a.backends import mail
    tid = envoyer(agent, commande())
    r1 = accuse(date_livraison_prevue="2026-11-20"); r1["lignes"][0]["quantite_confirmee"] = 5
    repondre(agent, "r1", r1); relire(agent)
    assert tache(agent, tid)["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    vrai = mail.Backend.acquitter
    monkeypatch.setattr(mail.Backend, "acquitter", lambda self, ids: (_ for _ in ()).throw(OSError("boîte injoignable")))
    repondre(agent, "r2", {"version": "1", "reference": "BC-26018-EXEMPLE", "date_livraison_prevue": "2026-11-20", "lignes": []})
    assert relire(agent) == 0
    repondre(agent, "r3", {"version": "1", "reference": "BC-26018-EXEMPLE", "date_livraison_prevue": "2026-11-27", "lignes": []})
    assert relire(agent) == 1
    monkeypatch.setattr(mail.Backend, "acquitter", vrai)
    assert relire(agent) == 0
    t = tache(agent, tid)
    assert "livraison décalée au 2026-11-27" in t["status"]["message"]["parts"][0]["text"]
    dates = [a["parts"][0]["data"].get("date_livraison_prevue") for a in t["artifacts"] if a["name"] == "accuse_reception"]
    assert dates == ["2026-11-20", "2026-11-27"]


def test_un_complement_qui_ne_donne_que_la_tva_est_garde(agent):
    tid = envoyer(agent, commande())
    r1 = accuse(date_livraison_prevue="2026-11-20"); r1["lignes"][0]["quantite_confirmee"] = 5
    repondre(agent, "r1", r1); relire(agent)
    repondre(agent, "r2", {"version": "1", "reference": "BC-26018-EXEMPLE",
                           "lignes": [{"id_ligne": "1", "cip13": "3400000000011", "taux_tva": 5.5}]})
    assert relire(agent) == 1
    t = tache(agent, tid)
    assert "TVA 5.5 %" in t["status"]["message"]["parts"][0]["text"]
    assert len([a for a in t["artifacts"] if a["name"] == "accuse_reception"]) == 2


def test_une_reponse_retenue_par_une_panne_retient_les_suivantes(agent):
    """Le premier mail (livraison le 20) ne peut pas être transcrit tout de suite ; le second
    (décalée au 27) arrive dans la même passe. Le second attend le premier : quand les deux
    passent, la dernière date connue est bien le 27."""
    tid = envoyer(agent, commande())
    d = agent.sorties / "reponses"; d.mkdir(parents=True, exist_ok=True)
    (d / "r1.eml").write_bytes(EML)                       # pas encore d'accusé préparé : il attend
    repondre(agent, "r2", {"version": "1", "reference": "BC-26018-EXEMPLE", "numero_commande_labo": "SO-1",
                           "date_livraison_prevue": "2026-11-27", "lignes": []})
    assert relire(agent) == 0
    assert tache(agent, tid)["status"]["state"] == "TASK_STATE_WORKING"
    (d / "r1.accuse.json").write_text(json.dumps(accuse(numero_commande_labo="SO-1", date_livraison_prevue="2026-11-20")), encoding="utf-8")
    relire(agent)
    t = tache(agent, tid)
    fil = [t] + suites(agent, t)
    dates = [a["parts"][0]["data"].get("date_livraison_prevue") for x in fil for a in x.get("artifacts") or [] if a["name"] == "accuse_reception"]
    assert dates[-1] == "2026-11-27" and dates[0] == "2026-11-20"


def test_un_second_mail_qui_ne_donne_que_des_conditions_est_garde(agent):
    """Après une première confirmation, le fournisseur n'écrit qu'un nouveau franco et un délai :
    le complément est enregistré ; un franco différent du précédent revient au pharmacien."""
    tid = envoyer(agent, commande())
    repondre(agent, "r1", accuse(numero_commande_labo="SO-1", date_livraison_prevue="2026-11-20",
                                 conditions={"franco_ht": 250})); relire(agent)
    premiere = tache(agent, tid)
    assert premiere["status"]["state"] == "TASK_STATE_COMPLETED"
    repondre(agent, "r2", {"version": "1", "reference": "BC-26018-EXEMPLE", "lignes": [],
                           "conditions": {"delai_livraison_jours": 4}})
    assert relire(agent) == 1
    [s] = suites(agent, premiere)
    texte = s["status"]["message"]["parts"][0]["text"]
    assert "livraison sous 4 j" in texte and s["status"]["state"] == "TASK_STATE_COMPLETED"
    repondre(agent, "r3", {"version": "1", "reference": "BC-26018-EXEMPLE", "lignes": [],
                           "conditions": {"franco_ht": 300}})
    assert relire(agent) == 1
    s2 = [x for x in suites(agent, premiere) if x["id"] != s["id"]] + suites(agent, s)
    assert any("franco 300 € HT au lieu de franco 250 € HT" in x["status"]["message"]["parts"][0]["text"]
               and x["status"]["state"] == "TASK_STATE_INPUT_REQUIRED" for x in s2)
