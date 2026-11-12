"""Revue du 4 novembre : ce qui sert à reconnaître une commande n'autorise jamais à la lire.
Deux officines sous la même référence chez le même fournisseur ne se mélangent pas."""
from __future__ import annotations
from types import SimpleNamespace
from a2a.types import TaskState
from pharma2a import relecture


def tache(ident, etat):
    return SimpleNamespace(id=ident, status=SimpleNamespace(state=etat))


def test_une_reponse_sans_fil_reste_ambigue_entre_deux_officines():
    """A a déjà sa réponse, B attend la sienne, même référence : un complément qui ne cite pas
    son fil n'est pas donné à B parce que sa tâche attend ; il reste ambigu, donc non rattaché."""
    a = (tache("A", TaskState.TASK_STATE_COMPLETED), {"reference": "BC-1", "identites": ["compte:111"]})
    b = (tache("B", TaskState.TASK_STATE_WORKING), {"reference": "BC-1", "identites": ["compte:222"]})
    r = SimpleNamespace(en_reponse_a=[], fil=[], accuse={"reference": "BC-1"}, reference="BC-1",
                        reference_explicite=True, objet="", texte="", de="adv@labo.example")
    out = relecture.candidates_de(r, {}, set(), {"BC-1": [a, b]}, SimpleNamespace(destinataire="", copie="", annuaire=None))
    assert {t.id for t, _ in out} == {"A", "B"}


def test_sans_fil_deux_commandes_de_la_meme_officine_restent_ambigues():
    """Même officine, une commande répondue et une qui attend : sans le fil, on ne choisit pas
    pour la réponse (une seule commande possible ou rien). L'espace en bout de référence ne gêne pas."""
    a = (tache("A", TaskState.TASK_STATE_COMPLETED), {"reference": "BC-1", "identites": ["compte:111"]})
    b = (tache("B", TaskState.TASK_STATE_WORKING), {"reference": "BC-1", "identites": ["compte:111"]})
    r = SimpleNamespace(en_reponse_a=[], fil=[], accuse={"reference": "BC-1"}, reference="BC-1 ",
                        reference_explicite=True, objet="", texte="", de="adv@labo.example")
    out = relecture.candidates_de(r, {}, set(), {"BC-1": [a, b]}, SimpleNamespace(destinataire="", copie="", annuaire=None))
    assert {t.id for t, _ in out} == {"A", "B"}


def _deux_officines(tmp_path):
    from starlette.testclient import TestClient
    from pharma2a import jetons
    from pharma2a.config import Config
    from pharma2a.serveur import creer_app
    reg = tmp_path / "jetons.json"
    ja, jb = jetons.creer(reg, "111111", "Pharmacie A"), jetons.creer(reg, "222222", "Pharmacie B")
    cfg = Config(jetons=str(reg), fournisseur="LABO UN", brouillon=True, sorties=tmp_path / "s")
    h = lambda j: {"Authorization": f"Bearer {j}", "A2A-Version": "1.0"}
    return TestClient(creer_app(cfg)), h(ja), h(jb)


def test_un_registre_et_un_jeton_unique_ne_se_melangent_pas(tmp_path):
    """Un jeton qui vaudrait pour tout compte à côté du registre mêlerait les officines (et
    passer de l'un à l'autre renverrait une commande) : l'agent ne démarre pas ainsi."""
    import pytest
    from pharma2a.config import Config
    from pharma2a.serveur import creer_app
    with pytest.raises(SystemExit, match="pas les deux"):
        creer_app(Config(jeton="global", jetons=str(tmp_path / "j.json"), brouillon=True, sorties=tmp_path / "s"))


def test_deux_officines_au_meme_cip_et_a_la_meme_reference_restent_separees(tmp_path):
    """A et B, deux comptes prouvés, la même référence et le même CIP déclaré : la commande de
    B est une autre commande, B ne lit pas celle de A, et une relance de B ne la retrouve pas."""
    import json
    from tests.test_agent import envoi, exemple
    c, ha, hb = _deux_officines(tmp_path)
    with c:
        doc_a = exemple("commande"); doc_a["officine"]["numero_compte_client"] = "111111"
        doc_b = exemple("commande"); doc_b["officine"]["numero_compte_client"] = "222222"   # même CIP, même référence
        ta = c.post("/message:send", json=envoi("commande", doc_a), headers=ha).json()["task"]["id"]
        assert c.get(f"/tasks/{ta}", headers=hb).status_code == 404
        # B relance une commande qu'il n'a jamais passée, sous la référence de A : rien de A n'est retrouvé ni copié
        relance = exemple("relance"); relance["officine"]["numero_compte_client"] = "222222"
        relance["reference"] = doc_a["reference"]
        r = c.post("/message:send", json=envoi("relance", relance), headers=hb).json()["task"]
        assert r["status"]["state"] == "TASK_STATE_REJECTED" and ta not in json.dumps(r)
        # sa propre commande sous la même référence est une autre commande
        tb = c.post("/message:send", json=envoi("commande", doc_b), headers=hb).json()["task"]
        assert tb["id"] != ta and tb["status"]["state"] == "TASK_STATE_WORKING"


def test_une_reponse_sans_fil_reste_ambigue_entre_deux_comptes_au_meme_cip():
    a = (tache("A", TaskState.TASK_STATE_COMPLETED), {"reference": "BC-1", "officine": "compte:111", "identites": ["cip:2000001"]})
    b = (tache("B", TaskState.TASK_STATE_WORKING), {"reference": "BC-1", "officine": "compte:222", "identites": ["cip:2000001"]})
    r = SimpleNamespace(en_reponse_a=[], fil=[], accuse={"reference": "BC-1"}, reference="BC-1",
                        reference_explicite=True, objet="", texte="", de="adv@labo.example")
    out = relecture.candidates_de(r, {}, set(), {"BC-1": [a, b]}, SimpleNamespace(destinataire="", copie="", annuaire=None))
    assert {t.id for t, _ in out} == {"A", "B"}


def test_une_tache_qui_change_de_page_pendant_la_lecture_est_retrouvee():
    """Un accusé arrive entre deux pages : la tâche remonte avant le curseur et n'apparaît sur
    aucune page de cette lecture. La commande n'est pas prise pour neuve : la relecture la voit."""
    import asyncio
    from types import SimpleNamespace as NS
    from a2a.types import Task, TaskStatus
    from pharma2a.dictionnaire import identites
    from pharma2a.relecture import contexte_robot
    from pharma2a.serveur import commande_deja_recue
    from tests.test_agent import exemple
    doc = exemple("commande")
    cible = Task(id="cible", context_id="c", status=TaskStatus(state=TaskState.TASK_STATE_WORKING))
    cible.metadata.update({"agent": "", "reference": doc["reference"], "nature": "commande", "identites": identites(doc)})
    autres = [Task(id=f"t{i}", context_id="c", status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED)) for i in range(3)]

    class Magasin:
        lectures = 0
        async def list(self, req, ctx):
            # première lecture : la cible a bougé entre les pages, elle n'est sur aucune
            Magasin.lectures += 1
            premiere = Magasin.lectures <= 2
            pages = {"": (autres[:2] if premiere else [cible] + autres[:2], "p2"), "p2": (autres[2:], "")}
            taches, suite = pages[req.page_token]
            return NS(tasks=taches, next_page_token=suite)

    deja, _ = asyncio.run(commande_deja_recue(Magasin(), "", doc, contexte_robot()))
    assert deja is not None and deja.id == "cible"


def test_l_adresse_exacte_du_fournisseur_passe_meme_sur_un_domaine_partage():
    """L'agent écrit depuis une messagerie grand public, le délégué aussi : sa réponse passe,
    une autre adresse du même domaine non."""
    cfg = SimpleNamespace(expediteur="achats.pharmacie@messagerie.example", copie="", destinataire="", annuaire=None)
    assert relecture.vient_du_fournisseur("Délégué <delegue.labo@messagerie.example>", ["delegue.labo@messagerie.example"], cfg)
    assert not relecture.vient_du_fournisseur("inconnu@messagerie.example", ["delegue.labo@messagerie.example"], cfg)
    assert not relecture.vient_du_fournisseur("achats.pharmacie@messagerie.example", ["delegue.labo@messagerie.example"], cfg)


def test_deux_agents_fournisseurs_avec_le_meme_compte_restent_separes():
    """Dans un annuaire, deux fournisseurs ont chacun un client « 123 » et une commande BC-1 :
    un complément sans fil reste ambigu entre les deux agents."""
    a = (tache("A", TaskState.TASK_STATE_COMPLETED), {"reference": "BC-1", "agent": "labo-a", "officine": "compte:123", "identites": ["compte:123"]})
    b = (tache("B", TaskState.TASK_STATE_WORKING), {"reference": "BC-1", "agent": "labo-b", "officine": "compte:123", "identites": ["compte:123"]})
    r = SimpleNamespace(en_reponse_a=[], fil=[], accuse={"reference": "BC-1"}, reference="BC-1",
                        reference_explicite=True, objet="", texte="", de="adv@labo.example")
    out = relecture.candidates_de(r, {}, set(), {"BC-1": [a, b]}, SimpleNamespace(destinataire="", copie="", annuaire=None))
    assert {t.id for t, _ in out} == {"A", "B"}


def _app(tmp_path, **k):
    from starlette.testclient import TestClient
    from pharma2a.config import Config
    from pharma2a.serveur import creer_app
    return TestClient(creer_app(Config(fournisseur="LABO UN", brouillon=True, sorties=tmp_path / "s",
                                       base=str(tmp_path / "taches.db"), **k)))


def test_changer_de_mode_de_jeton_n_expose_ni_ne_renvoie(tmp_path):
    """La même base SQLite, un registre puis un jeton unique, puis l'inverse : la liste du jeton
    unique ne rend pas les tâches des comptes du registre, et une commande passée sous le jeton
    unique ne repart pas quand son compte revient avec un jeton du registre."""
    from pharma2a import jetons
    from tests.test_agent import envoi, exemple
    reg = tmp_path / "jetons.json"
    ja = jetons.creer(reg, "111111", "Pharmacie A")
    h = lambda j: {"Authorization": f"Bearer {j}", "A2A-Version": "1.0"}
    doc = exemple("commande"); doc["officine"]["numero_compte_client"] = "111111"
    with _app(tmp_path, jetons=str(reg)) as c:
        assert c.post("/message:send", json=envoi("commande", doc), headers=h(ja)).status_code == 200
    with _app(tmp_path, jeton="unique") as c:
        liste = c.get("/tasks", params={"includeArtifacts": "true"}, headers=h("unique")).json()
        assert liste.get("tasks", []) == []
        autre = exemple("commande") | {"reference": "BC-UNIQUE"}; autre["officine"]["numero_compte_client"] = "111111"
        assert c.post("/message:send", json=envoi("commande", autre), headers=h("unique")).status_code == 200
    mails = len(list((tmp_path / "s").glob("*.eml")))
    with _app(tmp_path, jetons=str(reg)) as c:
        r = c.post("/message:send", json=envoi("commande", autre), headers=h(ja))
        assert r.status_code == 400 and "changement de jeton" in r.text
    assert len(list((tmp_path / "s").glob("*.eml"))) == mails


def test_une_commande_manquee_par_tous_les_parcours_est_retrouvee_par_sa_cle():
    """La tâche remonte devant le curseur à chaque parcours : aucun ne la voit. L'index de la clé
    de commande y mène quand même, et rien ne repart."""
    import asyncio
    from types import SimpleNamespace as NS
    from a2a.types import Task, TaskStatus
    from pharma2a.dictionnaire import identites
    from pharma2a.index_commandes import Index
    from pharma2a.relecture import contexte_robot
    from pharma2a.serveur import commande_deja_recue
    from tests.test_agent import exemple
    doc = exemple("commande")
    cible = Task(id="cible", context_id="c", status=TaskStatus(state=TaskState.TASK_STATE_WORKING))
    cible.metadata.update({"agent": "", "officine": "", "reference": doc["reference"], "nature": "commande", "identites": identites(doc)})

    class Magasin:
        async def list(self, req, ctx):
            return NS(tasks=[], next_page_token="")         # elle n'apparaît jamais
        async def get(self, tid, ctx):
            return cible if tid == "cible" else None

    index = Index()
    index.ajouter(Index.cle("", "", doc["reference"], "commande"), "cible")
    deja, _ = asyncio.run(commande_deja_recue(Magasin(), "", doc, contexte_robot(), None, index))
    assert deja is not None and deja.id == "cible"


def test_la_base_refuse_un_second_envoi_vivant_pour_la_meme_officine(tmp_path):
    """Le registre des envois : pour une officine prouvée, la même référence et la même nature
    n'ont qu'un envoi vivant ; une fois l'envoi fermé, la référence peut repartir. Sous le jeton
    unique (officine vide), le registre ne contraint pas."""
    from pharma2a.index_commandes import Index
    idx = Index(str(tmp_path / "taches.db"))
    cle = Index.cle("", "compte:111", "BC-1", "commande")
    assert idx.reserver(cle, "t1") and not idx.reserver(cle, "t2")
    assert idx.reserver(cle, "t1")                    # la même tâche se réserve à nouveau sans erreur
    idx.fermer("t1")
    assert idx.reserver(cle, "t2")
    assert Index(str(tmp_path / "taches.db")).taches(cle) == ["t1", "t2"]     # durable sur disque
    libre = Index.cle("", "", "BC-1", "commande")
    assert idx.reserver(libre, "u1") and idx.reserver(libre, "u2")


def test_un_mail_retarde_d_une_officine_fermee_ne_va_pas_a_l_autre():
    """A a porté BC-1 (tâche fermée, sortie de l'écoute), B l'a aussi et attend. Un mail sans fil
    sur BC-1 n'est pas donné à B : l'historique dit que deux officines ont porté la référence."""
    b = (tache("B", TaskState.TASK_STATE_WORKING), {"reference": "BC-1", "agent": "", "officine": "compte:222", "identites": ["compte:222"]})
    r = SimpleNamespace(en_reponse_a=[], fil=[], accuse={"reference": "BC-1"}, reference="BC-1",
                        reference_explicite=True, objet="", texte="", de="adv@labo.example")
    cfg = SimpleNamespace(destinataire="", copie="", annuaire=None)
    histo = {"BC-1": {("", "compte:111", "adv@labo.example"), ("", "compte:222", "adv@labo.example")}}
    out = relecture.candidates_de(r, {}, set(), {"BC-1": [b]}, cfg, histo)
    assert len(out) == 2 and relecture.AMBIGU in out
    # une seule officine dans l'historique : B la reçoit
    assert [t.id for t, _ in relecture.candidates_de(r, {}, set(), {"BC-1": [b]}, cfg, {"BC-1": {("", "compte:222", "adv@labo.example")}})] == ["B"]
    # un autre fournisseur (autre agent, autre adresse) a porté la même référence : pas une ambiguïté pour celui-ci
    assert [t.id for t, _ in relecture.candidates_de(r, {}, set(), {"BC-1": [b]}, cfg,
            {"BC-1": {("", "compte:222", "adv@labo.example"), ("labo-x", "compte:111", "adv@labo-x.example")}})] == ["B"]


def test_deux_agents_du_meme_fournisseur_ne_se_passent_pas_un_mail_retarde():
    """Deux agents de l'annuaire, un par officine, écrivent à la même adresse de fournisseur. A
    (agent ag-a) est fermée, B (ag-b) attend sous la même référence : le mail retardé de A, sans
    fil, n'est pas donné à B."""
    b = (tache("B", TaskState.TASK_STATE_WORKING), {"reference": "BC-1", "agent": "ag-b", "officine": "", "identites": ["cip:2"],
                                                     "destinataires": ["adv@labo.example"]})
    r = SimpleNamespace(en_reponse_a=[], fil=[], accuse={"reference": "BC-1"}, reference="BC-1",
                        reference_explicite=True, objet="", texte="", de="adv@labo.example")
    cfg = SimpleNamespace(destinataire="", copie="", annuaire=None)
    histo = {"BC-1": {("ag-a", "", "adv@labo.example"), ("ag-b", "", "adv@labo.example")}}
    out = relecture.candidates_de(r, {}, set(), {"BC-1": [b]}, cfg, histo)
    assert len(out) == 2 and relecture.AMBIGU in out


def test_le_registre_connait_les_porteurs_meme_si_le_parcours_les_manque(tmp_path):
    """L'historique de « qui a porté cette référence » vient aussi du registre durable : une tâche
    que le parcours paginé manque y est, et le registre apprend au passage celles qu'il croise."""
    import asyncio
    from types import SimpleNamespace as NS
    from a2a.types import Task, TaskStatus
    from pharma2a.index_commandes import Index
    idx = Index(str(tmp_path / "t.db"))
    idx.inscrire(Index.cle("", "compte:111", "BC-1", "commande"), "a", False, "adv@labo.example")
    t = Task(id="b", context_id="c", status=TaskStatus(state=TaskState.TASK_STATE_WORKING))
    t.metadata.update({"reference": "BC-1", "officine": "compte:222", "message_id": "<b@x>", "destinataires": ["adv@labo.example"]})

    class Magasin:
        async def list(self, req, ctx):
            return NS(tasks=[t], next_page_token="")

    mids, histo = asyncio.run(relecture.envois_clos(Magasin(), {"<b@x>"}, idx))
    assert {(a, o) for a, o, _ in histo["BC-1"]} == {("", "compte:111"), ("", "compte:222")}
    assert {o for _, o, _ in Index(str(tmp_path / "t.db")).porteurs("BC-1")} == {"compte:111", "compte:222"}


def test_passer_du_jeton_unique_sans_compte_au_registre_ne_renvoie_pas(tmp_path):
    """Commande sous le jeton unique avec numero_compte_client à null, puis le registre et la
    même commande avec son compte : le CIP et l'adresse la relient, elle ne repart pas."""
    from pharma2a import jetons
    from tests.test_agent import envoi, exemple
    h = lambda j: {"Authorization": f"Bearer {j}", "A2A-Version": "1.0"}
    doc = exemple("commande"); doc["officine"]["numero_compte_client"] = None
    with _app(tmp_path, jeton="unique") as c:
        assert c.post("/message:send", json=envoi("commande", doc), headers=h("unique")).status_code == 200
    mails = len(list((tmp_path / "s").glob("*.eml")))
    reg = tmp_path / "jetons.json"
    ja = jetons.creer(reg, "111111", "Pharmacie A")
    doc["officine"]["numero_compte_client"] = "111111"
    with _app(tmp_path, jetons=str(reg)) as c:
        r = c.post("/message:send", json=envoi("commande", doc), headers=h(ja))
        assert r.status_code == 400 and "autre jeton" in r.text
    assert len(list((tmp_path / "s").glob("*.eml"))) == mails


def test_une_condition_a_zero_est_une_condition():
    assert relecture.renseigne(0) and relecture.renseigne(0.0) and relecture.renseigne("x")
    assert not relecture.renseigne(None) and not relecture.renseigne(False) and not relecture.renseigne("")
    change, complete = relecture.ecart({"lignes": []}, {"lignes": [], "conditions": {"franco_ht": 0, "delai_livraison_jours": 0}}, None)
    assert "franco 0 € HT" in complete and "livraison sous 0 j" in complete and not change


def test_un_mail_qui_cite_un_fil_inconnu_n_est_pas_rattache_par_la_reference():
    b = (tache("B", TaskState.TASK_STATE_WORKING), {"reference": "BC-1", "agent": "", "officine": "compte:222", "identites": ["compte:222"]})
    r = SimpleNamespace(en_reponse_a=["<inconnu@labo.example>"], fil=[], accuse=None, reference="BC-1",
                        reference_explicite=True, objet="RE: BC-1", texte="", de="adv@labo.example")
    assert relecture.candidates_de(r, {}, set(), {"BC-1": [b]}, SimpleNamespace(destinataire="", copie="", annuaire=None)) == []


def test_revenir_du_registre_au_jeton_unique_ne_renvoie_pas_la_commande(tmp_path):
    from pharma2a import jetons
    from tests.test_agent import envoi, exemple
    reg = tmp_path / "jetons.json"
    ja = jetons.creer(reg, "111111", "Pharmacie A")
    h = lambda j: {"Authorization": f"Bearer {j}", "A2A-Version": "1.0"}
    doc = exemple("commande"); doc["officine"]["numero_compte_client"] = "111111"
    with _app(tmp_path, jetons=str(reg)) as c:
        assert c.post("/message:send", json=envoi("commande", doc), headers=h(ja)).status_code == 200
    mails = len(list((tmp_path / "s").glob("*.eml")))
    with _app(tmp_path, jeton="unique") as c:
        r = c.post("/message:send", json=envoi("commande", doc), headers=h("unique"))
        assert r.status_code == 400 and "autre jeton" in r.text
    assert len(list((tmp_path / "s").glob("*.eml"))) == mails


def test_un_agent_seul_ne_lit_pas_les_taches_d_un_autre_fournisseur():
    from a2a.types import Task
    from pharma2a.serveur import Guichet
    g = Guichet.__new__(Guichet); g.agent = ""
    t = Task(id="x"); t.metadata.update({"agent": "labo-a", "officine": "", "reference": "BC-1"})
    assert g._mienne(t) is False
    t2 = Task(id="y"); t2.metadata.update({"agent": "", "officine": "", "reference": "BC-1"})
    assert g._mienne(t2) is True
