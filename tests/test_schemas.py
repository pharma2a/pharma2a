"""Les cinq schémas valident leurs exemples, refusent un prix de l'officine, refusent l'inconnu.
Lancer, depuis pharma2a/ : python -m pytest"""
import json
import pathlib
import copy
from jsonschema import Draft202012Validator, FormatChecker

RACINE = pathlib.Path(__file__).resolve().parent.parent / "schema"
NOMS = ["commande", "accuse_reception", "etat_de_rayon", "fiche_fournisseur", "relance"]


def charger(nom):
    schema = json.loads((RACINE / f"{nom}.json").read_text())
    exemple = json.loads((RACINE / "exemples" / f"{nom}.json").read_text())
    return Draft202012Validator(schema, format_checker=FormatChecker()), schema, exemple


def erreurs(v, doc):
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '/'} : {e.message}" for e in v.iter_errors(doc)]


def test_schemas_bien_formes():
    for nom in NOMS:
        _, schema, _ = charger(nom)
        Draft202012Validator.check_schema(schema)


def test_exemples_valides():
    for nom in NOMS:
        v, _, ex = charger(nom)
        assert erreurs(v, ex) == [], nom


# Le franco est une condition du laboratoire : il figure dans l'accusé de réception.
# L'officine le rappelle tel quel dans sa commande, quand son agent a vérifié
# que la commande l'atteint (le laboratoire le demandait). Aucun prix d'achat ni de vente de
# l'officine, aucun montant de commande, nulle part.
# Les conditions du fournisseur, et elles seules : il donne son tarif dans sa fiche, son
# tarif, sa remise et son prix net dans son accusé ; l'officine rappelle seulement son franco
EXCEPTIONS = {"accuse_reception": {"franco_ht", "prix_tarif_ht", "remise_pct", "prix_net_ht"},
              "commande": {"franco_ht"},
              "fiche_fournisseur": {"franco_ht", "prix_tarif_ht", "tarif"}}
INTERDITS = ("prix", "tarif", "remise", "marge", "chiffre", "montant", "ht", "ttc", "pamp", "achat", "vente")


def test_aucun_prix_hors_des_conditions_du_fournisseur():
    for nom in NOMS:
        cles = [k.lower() for k in _cles(json.loads((RACINE / f"{nom}.json").read_text())) if k not in EXCEPTIONS.get(nom, set())]
        for mot in INTERDITS:
            assert not any(c == mot or c.startswith(mot + "_") or c.endswith("_" + mot) for c in cles), (nom, mot)


def test_les_messages_de_l_officine_ne_portent_aucun_prix():
    """La commande, l'état de rayon et la relance viennent de l'officine : aucun champ de prix,
    hormis le franco que le fournisseur a donné et que la commande rappelle."""
    for nom in ("commande", "etat_de_rayon", "relance"):
        cles = [k.lower() for k in _cles(json.loads((RACINE / f"{nom}.json").read_text()))]
        assert not [c for c in cles if any(m in c for m in ("prix", "tarif", "remise", "marge", "pamp"))], nom


def _cles(schema):
    out = []
    if isinstance(schema, dict):
        for k, v in schema.get("properties", {}).items():
            out.append(k); out += _cles(v)
        if "items" in schema: out += _cles(schema["items"])
    return out


def test_un_prix_est_refuse():
    v, _, ex = charger("commande")
    d = copy.deepcopy(ex); d["lignes"][0]["prix_ht"] = 12.5
    assert erreurs(v, d)
    d = copy.deepcopy(ex); d["remise_pct"] = 50
    assert erreurs(v, d)


def test_quantite_entiere_flottante_du_transport():
    """Part.data est un Struct protobuf : 6 ressort en 6.0. Le schéma l'accepte, 6.5 non."""
    v, _, ex = charger("commande")
    d = copy.deepcopy(ex); d["lignes"][0]["quantite"] = 6.0
    assert erreurs(v, d) == []
    d["lignes"][0]["quantite"] = 6.5
    assert erreurs(v, d)


def test_cip13_strict():
    v, _, ex = charger("commande")
    d = copy.deepcopy(ex); d["lignes"][0]["cip13"] = "340000000001"
    assert erreurs(v, d)


def test_inconnu_reste_null():
    v, _, ex = charger("accuse_reception")
    d = copy.deepcopy(ex); d["lignes"] = [{"id_ligne": "1", "cip13": "3400000000011"}]
    assert erreurs(v, d) == []
    d["date_livraison_prevue"] = "bientôt"
    assert erreurs(v, d)


def test_fiche_sans_prix_et_sans_assiette_devinee():
    """La fiche du fournisseur : la gamme, les colisages, les statuts, son tarif s'il le donne
    (prix_tarif_ht), jamais une remise ni un autre prix. L'assiette du franco se dit ou reste null."""
    v, _, ex = charger("fiche_fournisseur")
    d = copy.deepcopy(ex); d["gamme"][0]["prix_ht"] = 12.5
    assert erreurs(v, d)
    d = copy.deepcopy(ex); d["conditions"]["remise"] = 50
    assert erreurs(v, d)
    d = copy.deepcopy(ex); d["conditions"]["assiette_franco"] = "je ne sais pas"
    assert erreurs(v, d)
    d = copy.deepcopy(ex); d["conditions"]["assiette_franco"] = None
    assert erreurs(v, d) == []
    d = copy.deepcopy(ex); d["gamme"][2]["statut"] = "supprime"
    assert erreurs(v, d)
    d = copy.deepcopy(ex); d["contacts"]["delegues"][0]["secteur"] = ["4"]
    assert erreurs(v, d)
    # le strict minimum : qui et quand
    assert erreurs(v, {"version": "1", "fournisseur": {"nom": "X"}, "date": "2026-10-30"}) == []


# Les identifiants au-delà du CIP 13 : une ligne porte un cip13, un ean13 ou une
# ref_fournisseur, au moins un des trois ; le libellé reste obligatoire.

def test_une_ligne_sans_aucun_identifiant_est_refusee():
    for nom in ("commande", "accuse_reception", "etat_de_rayon"):
        v, _, ex = charger(nom)
        d = copy.deepcopy(ex)
        for k in ("cip13", "ean13", "ref_fournisseur"):
            d["lignes"][0].pop(k, None)
        assert erreurs(v, d), nom
        d["lignes"][0]["cip13"] = None
        assert erreurs(v, d), nom


def test_une_ligne_avec_ean13_seul_est_acceptee():
    v, _, ex = charger("commande")
    d = copy.deepcopy(ex); del d["lignes"][0]["cip13"]; d["lignes"][0]["ean13"] = "3600000000017"
    assert erreurs(v, d) == []
    d = copy.deepcopy(ex); del d["lignes"][0]["cip13"]; d["lignes"][0]["ref_fournisseur"] = "ACC-0042"
    assert erreurs(v, d) == []
    del d["lignes"][0]["libelle"]
    assert erreurs(v, d)
    v, _, ex = charger("etat_de_rayon")
    d = copy.deepcopy(ex); del d["lignes"][0]["cip13"]; d["lignes"][0]["ean13"] = "3600000000017"
    assert erreurs(v, d) == []
    v, _, ex = charger("accuse_reception")
    d = copy.deepcopy(ex); d["lignes"][0]["cip13"] = None; d["lignes"][0]["ean13"] = "3600000000017"
    assert erreurs(v, d) == []


# L'idempotence : la référence est l'identité de la commande, chaque ligne a son
# id_ligne, et l'accusé répond par cet id_ligne.

def test_reference_et_id_ligne_obligatoires():
    v, _, ex = charger("commande")
    d = copy.deepcopy(ex); del d["reference"]
    assert erreurs(v, d)
    d = copy.deepcopy(ex); del d["lignes"][0]["id_ligne"]
    assert erreurs(v, d)
    d = copy.deepcopy(ex); d["lignes"][0]["id_ligne"] = ""
    assert erreurs(v, d)
    v, _, ex = charger("accuse_reception")
    d = copy.deepcopy(ex); del d["lignes"][0]["id_ligne"]
    assert erreurs(v, d)


# L'origine de l'accusé : erp ou transcription, toujours dite.

def test_un_accuse_sans_origine_est_refuse():
    v, _, ex = charger("accuse_reception")
    d = copy.deepcopy(ex); del d["origine"]
    assert erreurs(v, d)
    d = copy.deepcopy(ex); d["origine"] = "humain"
    assert erreurs(v, d)
    for o in ("erp", "transcription"):
        d = copy.deepcopy(ex); d["origine"] = o
        assert erreurs(v, d) == []


def test_la_gamme_de_la_fiche_accepte_les_trois_identifiants():
    v, _, ex = charger("fiche_fournisseur")
    d = copy.deepcopy(ex); del d["gamme"][0]["cip13"]; d["gamme"][0]["ean13"] = "3600000000017"
    assert erreurs(v, d) == []
    d = copy.deepcopy(ex); del d["gamme"][0]["cip13"]
    assert erreurs(v, d)


def test_la_confirmation_globale_est_un_booleen_facultatif():
    v, _, ex = charger("accuse_reception")
    for val in (True, False, None):
        d = copy.deepcopy(ex); d["confirmation_globale"] = val
        assert erreurs(v, d) == []
    d = copy.deepcopy(ex); d["confirmation_globale"] = "oui"
    assert erreurs(v, d)


def test_etat_de_rayon_sans_couverture_si_zero_vente():
    v, _, ex = charger("etat_de_rayon")
    d = copy.deepcopy(ex); d["lignes"][0]["mois_a_zero_3_mois"] = 4
    assert erreurs(v, d)


if __name__ == "__main__":
    import sys
    ko = 0
    for n, f in list(globals().items()):
        if n.startswith("test_") and callable(f):
            try: f(); print("ok  ", n)
            except AssertionError as e: ko += 1; print("KO  ", n, e)
    sys.exit(1 if ko else 0)
