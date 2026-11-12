"""La pharmacie de démonstration face à un agent qui se conduit mal : une redirection vers un
autre domaine, une carte qui annonce ses opérations ailleurs qu'à la racine, un commentaire
qui contient des caractères de contrôle."""
from __future__ import annotations
import contextlib
import http.server
import json
import threading

import pytest

from pharma2a import officine

JETON = "jeton-factice-de-test"


class _Serveur(http.server.ThreadingHTTPServer):
    def __init__(self, routes):
        self.routes, self.recus = routes, []
        super().__init__(("127.0.0.1", 0), _Gestion)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}"


class _Gestion(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _repondre(self):
        self.server.recus.append((self.command, self.path, self.headers.get("Authorization")))
        n = int(self.headers.get("Content-Length") or 0)
        self.server.corps = json.loads(self.rfile.read(n)) if n else None
        r = self.server.routes.get((self.command, self.path)) or self.server.routes.get(("*", self.path))
        if r is None:
            self.send_response(404)
            self.end_headers()
            return
        code, corps, en_tetes = r() if callable(r) else r
        self.send_response(code)
        for k, v in (en_tetes or {}).items():
            self.send_header(k, v)
        b = json.dumps(corps).encode() if corps is not None else b""
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    do_GET = do_POST = _repondre


@contextlib.contextmanager
def serveur(routes):
    s = _Serveur(routes)
    t = threading.Thread(target=s.serve_forever, daemon=True)
    t.start()
    try:
        yield s
    finally:
        s.shutdown()
        s.server_close()


def tache(etat="TASK_STATE_COMPLETED", commentaire=None):
    lignes = [{"id_ligne": 1, "cip13": "3400000000011", "quantite_confirmee": 6.0, "commentaire": commentaire}]
    return {"id": "t1", "status": {"state": etat},
            "artifacts": [{"parts": [{"mediaType": officine.MEDIA["accuse_reception"],
                                      "data": {"origine": "fournisseur", "lignes": lignes}}]}]}


def test_la_carte_se_lit_sans_jeton():
    with serveur({("GET", "/.well-known/agent-card.json"): (200, {"name": "LABO"}, None)}) as s:
        officine.Agent(s.url, JETON).carte()
    assert s.recus == [("GET", "/.well-known/agent-card.json", None)]


def test_une_redirection_vers_un_autre_domaine_ne_recoit_jamais_le_jeton():
    with serveur({("*", "/tasks/t1"): (200, tache(), None)}) as piege:
        cible = piege.url.replace("127.0.0.1", "localhost") + "/tasks/t1"   # même machine, autre origine
        with serveur({("GET", "/tasks/t1"): (302, None, {"Location": cible})}) as agent:
            with pytest.raises(SystemExit, match="autre origine"):
                officine.Agent(agent.url, JETON).tache("t1")
    assert piege.recus == []
    assert agent.recus == [("GET", "/tasks/t1", f"Bearer {JETON}")]


def test_une_redirection_dans_la_meme_origine_est_suivie():
    routes = {("GET", "/tasks/t1"): (302, None, {"Location": "/v2/tasks/t1"}),
              ("GET", "/v2/tasks/t1"): (200, tache(), None)}
    with serveur(routes) as s:
        t = officine.Agent(s.url, JETON).tache("t1")
    assert t["id"] == "t1" and s.recus[-1] == ("GET", "/v2/tasks/t1", f"Bearer {JETON}")


def test_les_operations_vont_a_l_adresse_que_la_carte_annonce():
    routes = {}
    with serveur(routes) as s:
        routes[("GET", "/.well-known/agent-card.json")] = (200, {"name": "LABO", "supportedInterfaces": [
            {"url": s.url + "/rpc", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
            {"url": s.url + "/api", "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"}]}, None)
        routes[("POST", "/api/message:send")] = (200, {"task": tache("TASK_STATE_SUBMITTED")}, None)
        routes[("GET", "/api/tasks/t1")] = (200, tache(), None)
        assert officine.main(["--agent", s.url, "--jeton", JETON]) == 0
        assert officine.main(["--agent", s.url, "--jeton", JETON, "--tache", "t1"]) == 0
    chemins = [p for _, p, _ in s.recus]
    assert "/api/message:send" in chemins and "/api/tasks/t1" in chemins and "/message:send" not in chemins


def test_un_commentaire_du_fournisseur_n_envoie_aucune_sequence_au_terminal(capsys):
    officine.montrer(tache(commentaire="livré jeudi\u001b[2J\u009b31m"))
    sortie = capsys.readouterr().out
    assert "livré jeudi" in sortie and "\u001b" not in sortie and "\u009b" not in sortie


def test_une_carte_redirigee_vers_un_autre_domaine_n_y_envoie_ni_jeton_ni_commande():
    """La carte de A redirige vers B, dont la carte annonce sa propre interface : la redirection
    n'est pas suivie, B ne reçoit rien."""
    routes_b = {}
    with serveur(routes_b) as b:
        url_b = b.url.replace("127.0.0.1", "localhost")
        routes_b[("GET", "/.well-known/agent-card.json")] = (200, {"name": "B", "supportedInterfaces": [
            {"url": url_b, "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"}]}, None)
        routes_b[("POST", "/message:send")] = (200, {"task": tache()}, None)
        with serveur({("GET", "/.well-known/agent-card.json"): (302, None, {"Location": url_b + "/.well-known/agent-card.json"})}) as a:
            with pytest.raises(SystemExit, match="autre origine"):
                officine.main(["--agent", a.url, "--jeton", JETON])
    assert b.recus == []


def test_une_carte_qui_annonce_une_autre_origine_ne_detourne_pas_le_jeton(capsys):
    """Sans redirection, la carte de A annonce ses opérations chez B : ignoré, la commande va à A ;
    avec --autoriser B, elle va à B."""
    routes_a, routes_b = {}, {}
    with serveur(routes_b) as b, serveur(routes_a) as a:
        url_b = b.url.replace("127.0.0.1", "localhost")
        routes_a[("GET", "/.well-known/agent-card.json")] = (200, {"name": "A", "supportedInterfaces": [
            {"url": url_b, "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"}]}, None)
        routes_a[("POST", "/message:send")] = (200, {"task": tache()}, None)
        routes_b[("POST", "/message:send")] = (200, {"task": tache()}, None)
        assert officine.main(["--agent", a.url, "--jeton", JETON]) == 0
        assert b.recus == [] and ("POST", "/message:send", f"Bearer {JETON}") in a.recus
        assert "autre origine" in capsys.readouterr().err
        assert officine.main(["--agent", a.url, "--jeton", JETON, "--autoriser", url_b]) == 0
    assert ("POST", "/message:send", f"Bearer {JETON}") in b.recus


def test_un_refus_en_message_direct_s_affiche_sans_planter(capsys):
    """A2A permet de répondre à message:send par un message plutôt qu'une tâche : la démo le
    montre et s'arrête proprement. Elle demande aussi une réponse immédiate (returnImmediately)."""
    refus = {"message": {"role": "ROLE_AGENT", "parts": [{"text": "Compte client inconnu."}]}}
    with serveur({("GET", "/.well-known/agent-card.json"): (200, {"name": "LABO"}, None),
                  ("POST", "/message:send"): (200, refus, None)}) as s:
        assert officine.main(["--agent", s.url, "--jeton", JETON]) == 1
    assert "Compte client inconnu." in capsys.readouterr().out
    assert s.corps["configuration"] == {"returnImmediately": True}


def test_l_interface_de_la_version_annoncee_est_choisie_avec_son_tenant():
    """La carte liste 1.1 avant 1.0 : la démo prend 1.0, celle que ses requêtes annoncent, et
    met son tenant en tête du chemin."""
    routes = {}
    with serveur(routes) as s:
        routes[("GET", "/.well-known/agent-card.json")] = (200, {"name": "LABO", "supportedInterfaces": [
            {"url": s.url + "/v11", "protocolBinding": "HTTP+JSON", "protocolVersion": "1.1"},
            {"url": s.url + "/api", "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0", "tenant": "officine-42"}]}, None)
        routes[("POST", "/api/officine-42/message:send")] = (200, {"task": tache()}, None)
        assert officine.main(["--agent", s.url, "--jeton", JETON]) == 0
    assert ("POST", "/api/officine-42/message:send", f"Bearer {JETON}") in s.recus


def test_une_commande_qui_n_est_pas_un_objet_n_est_pas_envoyee(tmp_path, capsys):
    f = tmp_path / "vide.json"
    for contenu in ("[]", "null"):
        f.write_text(contenu)
        with serveur({("GET", "/.well-known/agent-card.json"): (200, {"name": "LABO"}, None)}) as s:
            assert officine.main(["--agent", s.url, "--jeton", JETON, "--commande", str(f), "--reference", "BC-1"]) == 2
        assert all(m != "POST" for m, _, _ in s.recus)
    assert "objet JSON" in capsys.readouterr().err


def test_le_jeton_et_la_commande_ne_partent_pas_en_clair_vers_une_autre_machine():
    a = officine.Agent("http://labo.example", JETON)
    with pytest.raises(SystemExit, match="HTTPS"):
        a.tache("t1")
    with pytest.raises(SystemExit, match="HTTPS"):
        officine.Agent("http://labo.example", None).envoyer({"reference": "BC-1"})
    assert officine._sur("https://labo.example") and officine._sur("http://127.0.0.1:8765") and officine._sur("http://localhost:8766")
