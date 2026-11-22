"""L'essai de conformité (python -m pharma2a.essai) : il relève un serveur qui plante, il
n'envoie aucune commande sans --commander, et l'agent d'exemple passe ce qu'il prétend tenir."""
from __future__ import annotations
import threading
import time

import pytest
import uvicorn

from pharma2a import essai
from pharma2a.dictionnaire import EXTENSION
from tests.test_officine import serveur
from tests.test_sample_supplier import sample


def carte(url):
    return {"name": "Agent d'essai", "capabilities": {"extensions": [{"uri": EXTENSION}]},
            "supportedInterfaces": [{"url": url, "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"}]}


def passer(url, commander, **k):
    e = essai.Essai(essai.Sonde(url, k.get("jeton")), k.get("compte"), k.get("cip"), k.get("ean"), "ESSAI-T", 0)
    e.passer(commander)
    return e.resultats


def test_un_serveur_qui_plante_sur_une_commande_est_un_ecart():
    with serveur({}) as s:
        s.routes[("GET", "/.well-known/agent-card.json")] = lambda: (200, carte(s.url), None)
        s.routes[("POST", "/message:send")] = (500, None, None)
        e = essai.Essai(essai.Sonde(s.url, "j"), "123456", "3400000000011", None, "ESSAI-T", 0)
        assert e.carte()
        assert e._ouverte("Une ligne par CIP 13", e._commande([{"cip13": "3400000000011"}])) is None
    plante = [x for x in e.resultats if x[0] == essai.ECART and "Une ligne par CIP 13" in x[2]]
    assert plante and "HTTP 500" in plante[0][3]


def test_sans_commander_aucune_commande_ne_part():
    with serveur({}) as s:
        s.routes[("GET", "/.well-known/agent-card.json")] = lambda: (200, carte(s.url), None)
        s.routes[("POST", "/message:send")] = (401, None, None)
        r = passer(s.url, False)
        envois = [x for x in s.recus if x[0] == "POST"]
    assert len(envois) == 1 and envois[0][2] is None          # le seul envoi : le message vide de la porte, sans jeton
    assert any(x[0] == essai.NON_VERIFIE and "commandes" in x[2].lower() for x in r)


def test_commander_sans_jeton_ni_compte_n_envoie_rien(capsys):
    assert essai.main(["--agent", "http://127.0.0.1:1", "--commander"]) == 2


def test_une_carte_qui_annonce_du_http_hors_de_la_machine_est_un_ecart():
    with serveur({}) as s:
        s.routes[("GET", "/.well-known/agent-card.json")] = lambda: (200, carte("http://agent.exemple.fr/a2a"), None)
        r = passer(s.url, False)
    assert any(x[0] == essai.ECART and "HTTPS" in x[2] for x in r)


@pytest.fixture
def exemple():
    conf = uvicorn.Config(sample.app(), host="127.0.0.1", port=8766, log_level="error")
    srv = uvicorn.Server(conf)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    fin = time.monotonic() + 10
    while not srv.started and time.monotonic() < fin:
        time.sleep(0.05)
    yield "http://127.0.0.1:8766"
    srv.should_exit = True
    t.join(5)


def test_l_agent_d_exemple_rend_des_accuses_conformes_et_rejette_le_hors_dictionnaire(exemple):
    r = {x[2]: x[0] for x in passer(exemple, True, jeton="j", compte="123456", cip="3400000000011", ean="3400000000011")}
    assert r["Une ligne par CIP 13 : l'accusé suit le dictionnaire et répond à la commande"] == essai.CONFORME
    assert r["Une commande hors dictionnaire (un prix sur une ligne) est rejetée"] == essai.CONFORME
    assert r["Un produit inconnu se dit sur sa ligne et revient au pharmacien"] == essai.CONFORME
    # ce que l'exemple laisse de côté, et dit laisser de côté : la porte
    assert r["Sans jeton, l'envoi d'un message est refusé"] == essai.ECART
