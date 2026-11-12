"""La relance : l'officine relance une commande déjà transmise, livraison en retard ou
sans réponse. Le mail part dans le fil de la commande, ne commande rien, et la réponse du
fournisseur revient sur la tâche de la relance."""
import email
import json
import uuid
from google.protobuf.json_format import MessageToDict
from a2a.helpers import new_data_message
from a2a.types import SendMessageRequest, Role
from pharma2a.dictionnaire import MEDIA, detecter, valider
from tests.test_relecture import agent, relire, EML, EXEMPLES, AUTH   # noqa: F401 (fixture)


def exemple(nom):
    return json.loads((EXEMPLES / f"{nom}.json").read_text(encoding="utf-8"))


def envoyer(c, nom, doc, headers=AUTH):
    m = new_data_message(doc, media_type=MEDIA[nom], role=Role.ROLE_USER); m.message_id = str(uuid.uuid4())
    r = c.post("/message:send", json=MessageToDict(SendMessageRequest(message=m)), headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["task"]


def emls(c):
    return sorted((c.sorties).glob("*.eml"), key=lambda p: p.stat().st_mtime_ns)


def test_le_message_relance_est_au_dictionnaire():
    assert valider("relance", exemple("relance")) == []
    assert detecter(None, None, exemple("relance")) == "relance"
    doc = exemple("relance"); doc["motif"] = "reclamation"
    assert valider("relance", doc)
    doc = exemple("relance"); doc["prix"] = 12
    assert valider("relance", doc)


def test_la_relance_part_dans_le_fil_de_la_commande(agent):
    cmd = envoyer(agent, "commande", exemple("commande"))
    mid_commande = cmd["metadata"]["message_id"]
    t = envoyer(agent, "relance", exemple("relance"))
    assert t["status"]["state"] == "TASK_STATE_WORKING", t["status"]
    assert t["metadata"]["nature"] == "relance" and t["metadata"]["relance_de"] == cmd["id"]
    m = email.message_from_bytes(emls(agent)[-1].read_bytes())
    assert m["Subject"].startswith("Relance : commande BC-26018-EXEMPLE")
    assert m["In-Reply-To"] == mid_commande
    corps = emls(agent)[-1].read_bytes().decode("utf-8", "replace")
    assert "30/10/2026" in corps and "ne passe pas de nouvelle commande" in corps
    assert "€" not in corps and "prix" not in corps.lower()
    # la commande, elle, n'a pas bougé
    assert agent.get(f"/tasks/{cmd['id']}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_WORKING"


def test_sans_commande_sous_cette_reference_la_relance_est_refusee(agent):
    doc = exemple("relance"); doc["reference"] = "BC-INCONNUE"
    t = envoyer(agent, "relance", doc)
    assert t["status"]["state"] == "TASK_STATE_REJECTED"
    assert not emls(agent)


def test_une_seule_relance_par_motif_et_par_jour(agent):
    envoyer(agent, "commande", exemple("commande"))
    assert envoyer(agent, "relance", exemple("relance"))["status"]["state"] == "TASK_STATE_WORKING"
    assert envoyer(agent, "relance", exemple("relance"))["status"]["state"] == "TASK_STATE_REJECTED"
    autre = exemple("relance"); autre["motif"] = "sans_reponse"; autre["date_livraison_annoncee"] = None
    assert envoyer(agent, "relance", autre)["status"]["state"] == "TASK_STATE_WORKING"


def test_une_autre_officine_ne_relance_pas_la_commande_d_une_autre(agent):
    envoyer(agent, "commande", exemple("commande"))
    doc = exemple("relance")
    doc["officine"] = {"nom": "Autre Officine", "numero_compte_client": "777",
                       "adresse_livraison": {"ligne1": "2 rue Autre", "code_postal": "75002", "ville": "Paris", "pays": "FR"}}
    assert envoyer(agent, "relance", doc)["status"]["state"] == "TASK_STATE_REJECTED"


def test_la_reponse_a_la_relance_revient_sur_la_relance(agent):
    cmd = envoyer(agent, "commande", exemple("commande"))
    rel = envoyer(agent, "relance", exemple("relance"))
    d = agent.sorties / "reponses"; d.mkdir(parents=True, exist_ok=True)
    (d / "r1.accuse.json").write_text(json.dumps({"version": "1", "reference": "BC-26018-EXEMPLE",
                                                   "date_livraison_prevue": "2026-11-05", "lignes": []}), encoding="utf-8")
    # sans le fil, une commande et sa relance en cours : on ne choisit pas pour la réponse, elle attend
    (d / "r1.eml").write_bytes(EML)
    assert relire(agent) == 0 and (d / "r1.eml").exists()
    # dans le fil de la relance, elle revient sur la relance
    mid = agent.get(f"/tasks/{rel['id']}", headers=AUTH).json()["metadata"]["message_id"]
    (d / "r1.eml").write_bytes(EML.replace(b"Subject:", f"In-Reply-To: {mid}\r\nSubject:".encode(), 1))
    assert relire(agent) == 1
    t = agent.get(f"/tasks/{rel['id']}", headers=AUTH).json()
    assert t["status"]["state"] == "TASK_STATE_COMPLETED"
    assert "relance répondue" in t["status"]["message"]["parts"][0]["text"]
    assert t["artifacts"][0]["parts"][0]["data"]["date_livraison_prevue"] == "2026-11-05"
    assert agent.get(f"/tasks/{cmd['id']}", headers=AUTH).json()["status"]["state"] == "TASK_STATE_WORKING"


def test_une_relance_ne_cree_jamais_de_commande(agent):
    cmd = envoyer(agent, "commande", exemple("commande"))
    envoyer(agent, "relance", exemple("relance"))
    again = envoyer(agent, "commande", exemple("commande"))
    assert again["id"] == cmd["id"]
    commandes = [p for p in emls(agent) if not email.message_from_bytes(p.read_bytes())["Subject"].startswith("Relance")]
    assert len(commandes) == 1


def test_une_commande_d_avant_les_identites_se_relance(agent):
    """Une commande du pilote : sa tâche n'avait ni « identites » ni « nature » en
    métadonnée, et la première relance réelle a été refusée. Les identités se relisent sur la commande."""
    cmd = envoyer(agent, "commande", exemple("commande"))
    store = agent.app_.state.store
    from a2a.server.context import ServerCallContext
    from a2a.auth.user import UnauthenticatedUser
    ctx = ServerCallContext(user=UnauthenticatedUser())
    async def vieillir():
        t = await store.get(cmd["id"], ctx)
        for cle in ("identites", "nature", "compte", "empreinte"):
            if cle in t.metadata:
                del t.metadata[cle]
        await store.save(t, ctx)
    agent.portal.call(vieillir)
    t = envoyer(agent, "relance", exemple("relance"))
    assert t["status"]["state"] == "TASK_STATE_WORKING", t["status"]

