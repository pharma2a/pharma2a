"""Sample supplier : le plus petit agent fournisseur qui parle PharmA2A.

Il reçoit une commande en A2A, la vérifie contre le dictionnaire, et répond aussitôt par un
accusé de réception ligne par ligne, depuis son petit stock. C'est un modèle, pas un produit :
un laboratoire ou un éditeur branche ici son vrai système de commandes (ERP, entrepôt).

    pip install -e .
    python examples/sample-supplier/agent.py                     sur http://127.0.0.1:8766
    python -m pharma2a.officine --agent http://127.0.0.1:8766 --attendre 2    la pharmacie de démo lui commande

Pour tenir en une page, il laisse de côté ce qu'un agent en service doit ajouter : l'authentification
(un jeton par officine) et l'idempotence (SPECIFICATION.md, section 8). Voir le supplier email bridge."""
from __future__ import annotations
import datetime as dt
import uuid
from a2a.helpers import get_data_parts, new_data_part, new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_jsonrpc_routes, create_rest_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentExtension, AgentInterface, AgentSkill
from starlette.applications import Starlette
from pharma2a.carte_route import route_carte
from pharma2a.dictionnaire import EXTENSION, MEDIA, valider

URL = "http://127.0.0.1:8766"

# Le stock du fournisseur, par CIP 13. Votre ERP le remplace.
STOCK = {"3400000000011": 120, "3400000000028": 4, "3400000000035": 0}

CARTE = AgentCard(
    name="Sample supplier",
    description="Agent fournisseur d'exemple : reçoit une commande PharmA2A et rend l'accusé de réception.",
    version="0.1.0",
    supported_interfaces=[AgentInterface(url=URL, protocol_binding="HTTP+JSON", protocol_version="1.0"),
                          AgentInterface(url=URL, protocol_binding="JSONRPC", protocol_version="1.0")],
    capabilities=AgentCapabilities(streaming=False, extensions=[AgentExtension(uri=EXTENSION, required=False)]),
    default_input_modes=["application/json"], default_output_modes=["application/json"],
    skills=[AgentSkill(id="commander", name="Commander", description="Commande PharmA2A, accusé de réception en retour.",
                       tags=["pharma2a", "commande"], input_modes=[MEDIA["commande"]], output_modes=[MEDIA["accuse_reception"]])],
)


def accuse(commande: dict) -> dict:
    """L'accusé de réception : chaque ligne dit ce qui part, ce qui suit en reliquat, ce qui
    manque. Il répond par l'id_ligne de la commande ; la commande, elle, ne porte jamais de prix."""
    lignes = []
    for l in commande["lignes"]:
        stock = STOCK.get(l.get("cip13") or l.get("ean13") or l.get("ref_fournisseur") or "", 0)
        part = min(l["quantite"], stock)
        # le reliquat ne suit que si l'officine l'accepte (la ligne prime sur la commande)
        suit = l.get("reliquat_accepte")
        suit = (commande.get("reliquat_accepte") if suit is None else suit) is not False
        # l'accusé reprend les identifiants produit de la ligne, quels qu'ils soient
        codes = {k: l[k] for k in ("cip13", "ean13", "ref_fournisseur") if l.get(k)}
        lignes.append({"id_ligne": l["id_ligne"], **codes, "quantite_confirmee": part,
                       "reliquat": l["quantite"] - part if part and suit else 0, "rupture": stock == 0})
    return {"version": "1", "reference": commande["reference"], "origine": "erp",
            "numero_commande_labo": "SO-" + uuid.uuid4().hex[:6].upper(),
            "date_livraison_prevue": (dt.date.today() + dt.timedelta(days=3)).isoformat(),
            "lignes": lignes}


class Fournisseur(AgentExecutor):
    async def execute(self, ctx: RequestContext, queue: EventQueue) -> None:
        tache = new_task_from_user_message(ctx.message)
        await queue.enqueue_event(tache)
        maj = TaskUpdater(queue, tache.id, tache.context_id)
        donnees = get_data_parts(ctx.message.parts)
        commande = donnees[0] if donnees else None
        erreurs = valider("commande", commande) if isinstance(commande, dict) else ["une commande est un objet JSON"]
        if not erreurs and commande.get("nature") == "proposition":
            erreurs = ["cet exemple ne répond pas à une proposition (SPECIFICATION.md, 7.3)"]
        if erreurs:
            # hors dictionnaire, ou une proposition : on refuse en disant pourquoi ; rien n'est commandé
            await maj.reject(maj.new_agent_message([new_text_part("Commande refusée : " + " ; ".join(erreurs[:5]))]))
            return
        a = accuse(commande)
        await maj.add_artifact([new_data_part(a, media_type=MEDIA["accuse_reception"])], name="accuse_reception",
                               extensions=[EXTENSION])
        manque = [l for l, c in zip(a["lignes"], commande["lignes"]) if l["quantite_confirmee"] != c["quantite"]]
        texte = f"Commande {a['numero_commande_labo']} enregistrée, livraison le {a['date_livraison_prevue']}."
        if manque:
            # une quantité qui n'est pas celle commandée : c'est au pharmacien de trancher
            await maj.requires_input(maj.new_agent_message([new_text_part(texte + f" {len(manque)} ligne(s) à trancher.")]))
        else:
            await maj.complete(maj.new_agent_message([new_text_part(texte)]))

    async def cancel(self, ctx: RequestContext, queue: EventQueue) -> None:
        await TaskUpdater(queue, ctx.task_id, ctx.context_id).cancel()


def app() -> Starlette:
    guichet = DefaultRequestHandler(agent_executor=Fournisseur(), task_store=InMemoryTaskStore(), agent_card=CARTE)
    return Starlette(routes=[route_carte(CARTE)] + create_rest_routes(guichet) + create_jsonrpc_routes(guichet, "/"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app(), host="127.0.0.1", port=8766)
