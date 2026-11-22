"""Le canal de démonstration (PHARMA2A_BACKEND=demo) : un fournisseur fictif qui répond seul,
par des accusés du dictionnaire, sans rien transmettre à personne."""
from __future__ import annotations
import json

from pharma2a.backends import demo
from pharma2a.dictionnaire import SCHEMAS, valider


def commande(**plus):
    c = json.loads((SCHEMAS / "exemples" / "commande.json").read_text(encoding="utf-8"))
    c.update(plus)
    return c


def test_la_commande_d_exemple_recoit_un_accuse_conforme_ligne_par_ligne():
    c = commande()
    a = demo.accuse(c)
    assert valider("accuse_reception", a) == [] and a["origine"] == "erp"
    assert [(l["id_ligne"], l["quantite_confirmee"], l["reliquat"], l["rupture"]) for l in a["lignes"]] == \
        [("1", 6, 0, False), ("2", 4, 17, False), ("3", 0, 0, True)]


def test_une_reference_absente_du_catalogue_n_est_ni_commandee_ni_en_rupture():
    c = commande()
    c["lignes"][0]["cip13"] = "3400000000999"
    l = demo.accuse(c)["lignes"][0]
    assert (l["quantite_confirmee"], l["rupture"]) == (0, False) and "catalogue" in l["commentaire"]


def test_une_proposition_recoit_colisage_et_conditions_sans_numero_de_commande():
    a = demo.accuse(commande(nature="proposition", questions=["franco", "colisage"]))
    assert valider("accuse_reception", a) == []
    assert "numero_commande_labo" not in a and "date_livraison_prevue" not in a
    assert a["conditions"]["franco_ht"] == 400 and a["lignes"][0]["colisage"] == 6
    assert all("quantite_confirmee" not in l for l in a["lignes"])


def test_le_canal_rend_la_reponse_jusqu_a_l_acquittement_et_ne_repond_pas_a_une_relance():
    b = demo.Backend(None)
    c = commande()
    b.transmettre("commande", c, c["reference"], "<m1@pharma2a>")
    b.transmettre("relance", {"reference": c["reference"]}, c["reference"], "<m2@pharma2a>")
    r = b.lire_reponses()
    assert len(r) == 1 and r[0].en_reponse_a == ["<m1@pharma2a>"] and b.lire_reponses()[0].identifiant == r[0].identifiant
    b.acquitter([r[0].identifiant])
    assert b.lire_reponses() == []
