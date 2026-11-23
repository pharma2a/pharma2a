"""Le réseau (python -m pharma2a.reseau) : l'annuaire ne dit que des domaines, tout se vérifie
chez le fournisseur, personne ne valide à la main ; l'index dit, pour un code, qui le vend."""
from __future__ import annotations

import copy
import json

from pharma2a import reseau
from pharma2a.dictionnaire import EXTENSION, SCHEMAS
from tests.test_officine import serveur

FICHE = json.loads((SCHEMAS / "exemples" / "fiche_fournisseur.json").read_text(encoding="utf-8"))
CARTE = {"name": "Agent", "capabilities": {"extensions": [{"uri": EXTENSION}]}}


def fournisseur(s, siren="000000000", fiche=None, carte=CARTE, prefixe="", declare=True):
    """Un agent de fournisseur sur le serveur s ; declare=True y dépose aussi la déclaration."""
    f = copy.deepcopy(fiche or FICHE)
    f["fournisseur"]["siren"] = siren
    s.routes[("GET", prefixe + "/.well-known/agent-card.json")] = (200, carte, None)
    s.routes[("GET", prefixe + "/fiche_fournisseur.json")] = (200, f, None)
    if declare:
        s.routes[("GET", "/.well-known/pharma2a.json")] = (200, {"siren": siren}, None)
    return s.url.removeprefix("http://")


def entree(domaine):
    return {"domaine": domaine}


def test_un_fournisseur_qui_se_declare_sur_son_domaine_est_verifie_et_ses_codes_entrent_dans_l_index(tmp_path):
    with serveur({}) as s:
        r = reseau.construire({"fournisseurs": [entree(fournisseur(s))]}, tmp_path / "reseau")
    assert (r["fournisseurs_verifies"], r["codes"]) == (1, 4)                  # cinq références, dont une arrêtée
    assert r["detail"][0]["nom"] == FICHE["fournisseur"]["nom"]                # le nom vient de sa fiche
    p = json.loads((tmp_path / "reseau" / "produits" / "3400000000011.json").read_text(encoding="utf-8"))
    assert [f["siren"] for f in p["fournisseurs"]] == ["000000000"] and p["fournisseurs"][0]["agent"] == s.url
    assert "prix" not in json.dumps(p)                       # jamais un prix dans l'index


def test_sans_declaration_sur_son_domaine_un_fournisseur_n_entre_pas(tmp_path):
    with serveur({}) as s:
        r = reseau.construire({"fournisseurs": [entree(fournisseur(s, declare=False))]}, tmp_path / "reseau")
    assert r["fournisseurs_verifies"] == 0 and "pas de déclaration" in r["detail"][0]["raison"]
    assert not list((tmp_path / "reseau" / "produits").iterdir())


def test_un_siren_de_fiche_different_de_la_declaration_est_ecarte():
    with serveur({}) as s:
        d = fournisseur(s, siren="999999999")
        s.routes[("GET", "/.well-known/pharma2a.json")] = (200, {"siren": "000000000"}, None)
        assert "SIREN de la fiche" in reseau.verifier(entree(d))["raison"]
        s.routes[("GET", "/.well-known/pharma2a.json")] = (200, {"siren": "12"}, None)
        assert "pas de SIREN" in reseau.verifier(entree(d))["raison"]


def test_une_carte_sans_l_extension_ou_une_fiche_absente_ecartent_le_fournisseur():
    with serveur({}) as s:
        d = fournisseur(s, carte={"name": "Agent", "capabilities": {}})
        assert "extension" in reseau.verifier(entree(d))["raison"]
    with serveur({}) as s:
        s.routes[("GET", "/.well-known/agent-card.json")] = (200, CARTE, None)
        s.routes[("GET", "/.well-known/pharma2a.json")] = (200, {"siren": "000000000"}, None)
        assert "fiche du fournisseur illisible" in reseau.verifier(entree(s.url.removeprefix("http://")))["raison"]


def test_un_agent_heberge_chez_un_integrateur_est_celui_que_designe_le_domaine_du_fournisseur():
    with serveur({}) as integrateur, serveur({}) as site:
        fournisseur(integrateur, siren="111111111", prefixe="/labo-deux", declare=False)
        agent = integrateur.url + "/labo-deux"
        site.routes[("GET", "/.well-known/pharma2a.json")] = (200, {"siren": "111111111", "agent": agent + "/"}, None)
        v = reseau.verifier(entree(site.url.removeprefix("http://")))
    assert v["verifie"] and v["agent"] == agent and v["siren"] == "111111111"


def test_deux_fournisseurs_du_meme_code_y_figurent_tous_les_deux_et_un_arret_n_y_entre_pas(tmp_path):
    arretee = copy.deepcopy(FICHE)
    arretee["gamme"][0]["date_arret"] = "2026-01-01"
    with serveur({}) as a, serveur({}) as b:
        annuaire = {"fournisseurs": [entree(fournisseur(a, siren="000000000")), entree(fournisseur(b, siren="222222222", fiche=arretee))]}
        r = reseau.construire(annuaire, tmp_path / "reseau")
        premier = json.loads((tmp_path / "reseau" / "produits" / f"{FICHE['gamme'][0]['cip13']}.json").read_text(encoding="utf-8"))
        second = json.loads((tmp_path / "reseau" / "produits" / f"{FICHE['gamme'][1]['cip13']}.json").read_text(encoding="utf-8"))
        # un second passage remplace l'index sans laisser de dossier de travail
        reseau.construire(annuaire, tmp_path / "reseau")
    assert r["fournisseurs_verifies"] == 2
    assert [f["siren"] for f in premier["fournisseurs"]] == ["000000000"]               # arrêtée chez le second
    assert [f["siren"] for f in second["fournisseurs"]] == ["000000000", "222222222"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["reseau"]


def test_un_siren_revendique_par_deux_domaines_n_entre_pour_aucun(tmp_path):
    with serveur({}) as vrai, serveur({}) as usurpateur:
        annuaire = {"fournisseurs": [entree(fournisseur(vrai)), entree(fournisseur(usurpateur))]}
        r = reseau.construire(annuaire, tmp_path / "reseau")
    assert r["fournisseurs_verifies"] == 0 and r["codes"] == 0
    assert all("plusieurs domaines" in f["raison"] for f in r["detail"])


def test_une_entree_mal_formee_ou_en_double_n_arrete_pas_le_tour(tmp_path):
    with serveur({}) as s:
        d = fournisseur(s)
        r = reseau.construire({"fournisseurs": [{"domaine": "pas un domaine"}, "n'importe quoi", entree(d), entree(d.upper())]}, tmp_path / "reseau")
    assert [f["verifie"] for f in r["detail"]] == [False, False, True]                # le doublon n'a qu'une ligne
