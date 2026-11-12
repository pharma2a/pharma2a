"""Conformité A2A 1.0.1 : la liste se filtre avant de se compter,
une commande répétée respecte la limite d'historique, la version se contrôle en majeure et
mineure. (La tâche terminée qui ne redémarre pas : tests/test_regles_pharmacien.py.)"""
import copy
import pytest
from tests.test_agent import envoi, exemple
from tests.test_jetons import agent, h   # noqa: F401 (fixture)


def commander(c, jeton, reference):
    doc = copy.deepcopy(exemple("commande")); doc["reference"] = reference
    r = c.post("/message:send", json=envoi("commande", doc), headers=h(jeton))
    assert r.status_code == 200, r.text
    return r.json()["task"]["id"]


# --- la liste

def test_une_officine_sans_tache_ne_compte_pas_celles_des_autres(agent):
    for i in range(3):
        commander(agent, agent.a, f"BC-{i}")
    r = agent.get("/tasks", headers=h(agent.b)).json()
    assert r.get("tasks", []) == [] and int(r.get("totalSize", 0)) == 0


def test_la_pagination_ne_compte_que_les_siennes(agent):
    ids = {commander(agent, agent.a, f"BC-{i}") for i in range(3)}
    vus, jeton = set(), None
    while True:
        params = {"pageSize": 2} | ({"pageToken": jeton} if jeton else {})
        r = agent.get("/tasks", params=params, headers=h(agent.a)).json()
        assert int(r["totalSize"]) == 3 and len(r["tasks"]) <= 2
        vus |= {t["id"] for t in r["tasks"]}
        jeton = r.get("nextPageToken")
        if not jeton:
            break
    assert vus == ids
    assert agent.get("/tasks", params={"pageToken": "nimporte"}, headers=h(agent.a)).status_code == 400


# --- la limite d'historique

def test_la_commande_repetee_respecte_history_length(agent):
    tid = commander(agent, agent.a, "BC-H")
    corps = envoi("commande", exemple("commande") | {"reference": "BC-H"})
    corps["configuration"] = {"historyLength": 0}
    r = agent.post("/message:send", json=corps, headers=h(agent.a)).json()
    assert r["task"]["id"] == tid and not r["task"].get("history")
    corps["configuration"] = {"historyLength": -1}
    assert agent.post("/message:send", json=corps, headers=h(agent.a)).status_code == 400


# --- la version

def test_une_version_mineure_inconnue_est_refusee(agent):
    for v in ("1.999", "1.1", "2.0", "1", "abc"):
        r = agent.get("/tasks", headers=h(agent.a) | {"A2A-Version": v})
        assert r.status_code >= 400 and "not supported" in r.text, (v, r.status_code, r.text)
        corps = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": envoi("commande", exemple("commande"))}
        r = agent.post("/", json=corps, headers=h(agent.a) | {"A2A-Version": v})
        assert r.json()["error"]["code"] == -32009, (v, r.text)
    assert not list(agent.sorties.glob("*.eml"))


def test_le_correctif_est_ignore(agent):
    r = agent.get("/tasks", headers=h(agent.a) | {"A2A-Version": "1.0.1"})
    assert r.status_code == 200, r.text


# --- les écritures concurrentes sur une même tâche

def test_la_repetition_qui_apprend_une_identite_n_efface_pas_l_accuse():
    """La répétition relisait la tâche « working », la relecture écrivait
    « completed » avec l'accusé, puis la répétition réécrivait sa vieille copie."""
    import asyncio
    from a2a.server.tasks import InMemoryTaskStore
    from a2a.types import Task, TaskStatus, TaskState
    from pharma2a.relecture import contexte_robot, verrou_tache
    from pharma2a.serveur import apprendre_identites, commande_deja_recue
    doc = exemple("commande")
    sans_cip = copy.deepcopy(doc); sans_cip["officine"]["cip_officine"] = None

    async def scenario():
        store, ctx = InMemoryTaskStore(), contexte_robot()
        t = Task(id="t1", context_id="c1", status=TaskStatus(state=TaskState.TASK_STATE_WORKING))
        from pharma2a.dictionnaire import identites
        t.metadata.update({"agent": "", "reference": doc["reference"], "nature": "commande", "identites": identites(sans_cip)})
        await store.save(t, ctx)
        deja, _ = await commande_deja_recue(store, "", doc, ctx)
        async with verrou_tache("t1"):
            apprend = asyncio.create_task(apprendre_identites(store, deja, doc, ctx, None))
            await asyncio.sleep(0.05)
            assert not apprend.done()               # elle attend que la relecture ait écrit
            fini = await store.get("t1", ctx)
            fini.status.state = TaskState.TASK_STATE_COMPLETED
            await store.save(fini, ctx)
        await apprend
        final = await store.get("t1", ctx)
        assert deja.id == "t1" and final.status.state == TaskState.TASK_STATE_COMPLETED
        assert "cip:2000001" in list(final.metadata["identites"])

    asyncio.run(scenario())


def test_une_repetition_sans_message_id_ou_sans_role_est_refusee(agent):
    tid = commander(agent, agent.a, "BC-E")
    for retire in ("messageId", "role"):
        corps = envoi("commande", exemple("commande") | {"reference": "BC-E"})
        del corps["message"][retire]
        rpc = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": corps}
        r = agent.post("/", json=rpc, headers=h(agent.a)).json()
        assert "error" in r and tid not in str(r), (retire, r)


def test_le_meme_produit_sur_deux_lignes_est_refuse(agent):
    doc = copy.deepcopy(exemple("commande"))
    doc["lignes"][1]["cip13"] = doc["lignes"][0]["cip13"]
    r = agent.post("/message:send", json=envoi("commande", doc), headers=h(agent.a)).json()
    assert r["task"]["status"]["state"] == "TASK_STATE_REJECTED"
    assert "un produit par ligne" in str(r["task"]["status"]["message"])
    assert not list(agent.sorties.glob("*.eml"))


def test_la_pagination_survit_a_un_changement_de_la_tache_d_ancrage(agent):
    ids = [commander(agent, agent.a, f"BC-P{i}") for i in range(4)]
    r = agent.get("/tasks", params={"pageSize": 2, "status": "TASK_STATE_WORKING"}, headers=h(agent.a)).json()
    premiers = [t["id"] for t in r["tasks"]]
    # la tâche d'ancrage quitte le filtre (annulée) avant la page suivante
    assert agent.post(f"/tasks/{premiers[-1]}:cancel", headers=h(agent.a)).status_code == 200
    r2 = agent.get("/tasks", params={"pageSize": 2, "status": "TASK_STATE_WORKING", "pageToken": r["nextPageToken"]}, headers=h(agent.a))
    assert r2.status_code == 200, r2.text
    assert set(premiers) | {t["id"] for t in r2.json()["tasks"]} == set(ids)


# --- robustesse du dictionnaire et des répétitions


@pytest.mark.parametrize("doc", [None, [], "x", 1])
def test_une_commande_qui_n_est_pas_un_objet_est_refusee_sans_planter(agent, doc):
    r = agent.post("/message:send", json=envoi("commande", doc), headers=h(agent.a))
    assert r.status_code == 200, r.text
    assert r.json()["task"]["status"]["state"] == "TASK_STATE_REJECTED"


def test_le_nom_du_fournisseur_se_compare_exactement():
    from pharma2a.executeur import _meme_fournisseur as m
    assert m("Laboratoires Labo Un", "LABO UN") and m("labo-un", "LABO UN")
    assert not m("LABO UN BIS", "LABO UN") and not m("UN", "LABO UN") and not m("AUTRE LABO", "LABO UN")


def test_des_valeurs_equivalentes_pour_le_schema_font_une_repetition(agent):
    from pharma2a.dictionnaire import empreinte
    doc = exemple("commande")
    sans_pays = copy.deepcopy(doc); del sans_pays["officine"]["adresse_livraison"]["pays"]
    assert empreinte(doc) == empreinte(doc | {"date_livraison_exigee": False}) == empreinte(sans_pays)
    assert empreinte(doc) != empreinte(doc | {"date_livraison_exigee": True})
    assert empreinte(doc) != empreinte(doc | {"reliquat_accepte": False})
    # « false » sur une ligne d'une commande déjà à « false » : la consigne qui s'applique est la même
    non = copy.deepcopy(doc) | {"reliquat_accepte": False}
    non_ligne = copy.deepcopy(non); non_ligne["lignes"][0]["reliquat_accepte"] = False
    assert empreinte(non) == empreinte(non_ligne)
    oui_ligne = copy.deepcopy(non); oui_ligne["lignes"][0]["reliquat_accepte"] = True
    assert empreinte(non) != empreinte(oui_ligne)
    ref_non = {"reference": doc["reference"] + "-NON"}
    # les questions d'une proposition comptent, l'ordre non
    assert empreinte(doc | {"questions": ["franco", "catalogue"]}) == empreinte(doc | {"questions": ["catalogue", "franco"]})
    assert empreinte(doc | {"questions": ["franco"]}) != empreinte(doc | {"questions": ["franco", "delai_livraison"]})
    t_non = agent.post("/message:send", json=envoi("commande", non | ref_non), headers=h(agent.a)).json()["task"]["id"]
    r = agent.post("/message:send", json=envoi("commande", non_ligne | ref_non), headers=h(agent.a)).json()
    assert r["task"]["id"] == t_non
    tid = commander(agent, agent.a, doc["reference"])
    r = agent.post("/message:send", json=envoi("commande", doc | {"date_livraison_exigee": False}), headers=h(agent.a)).json()
    assert r["task"]["id"] == tid


def test_le_flux_non_pris_en_charge_le_dit_sans_planter(agent):
    """Pas de flux (streaming: false) : une demande de flux rend UnsupportedOperationError,
    400 en HTTP+JSON et -32004 en JSON-RPC, jamais une erreur interne. Le contrôle de version
    posé devant chaque méthode ne doit pas casser les méthodes de flux du SDK."""
    r = agent.post("/message:stream", json=envoi("commande", exemple("commande")), headers=h(agent.a))
    assert r.status_code == 400, (r.status_code, r.text)
    corps = {"jsonrpc": "2.0", "id": 1, "method": "SendStreamingMessage", "params": envoi("commande", exemple("commande"))}
    r = agent.post("/", json=corps, headers=h(agent.a))
    assert r.json()["error"]["code"] == -32004, r.text
    r = agent.post("/", json=corps, headers=h(agent.a) | {"A2A-Version": "1.999"})
    assert r.json()["error"]["code"] == -32009, r.text
    assert not list(agent.sorties.glob("*.eml"))
