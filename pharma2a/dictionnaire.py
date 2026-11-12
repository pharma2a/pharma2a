"""Le dictionnaire PharmA2A : les cinq schémas, la validation, et la remise d'aplomb
des entiers que le transport protobuf rend flottants (6 -> 6.0)."""
from __future__ import annotations
import json
import pathlib
from functools import lru_cache
from jsonschema import Draft202012Validator, FormatChecker
from .identifiants import AMBIGU, IDENTIFIANTS, cles, code, indexer, retrouver  # noqa: F401 (l'API du dictionnaire)

# le dépôt (schema/ à la racine), sinon le paquet installé (pharma2a/schema)
SCHEMAS = next((d for d in (pathlib.Path(__file__).resolve().parent.parent / "schema",
                            pathlib.Path(__file__).resolve().parent / "schema") if (d / "commande.json").exists()),
               pathlib.Path(__file__).resolve().parent.parent / "schema")
NOMS = ("commande", "accuse_reception", "etat_de_rayon", "fiche_fournisseur", "relance")
# le type de média que chaque message porte dans sa partie de données
MEDIA = {n: f"application/pharma2a.{n}+json" for n in NOMS}
CLE_METADATA = "pharma2a.message"   # l'autre façon de dire quel message on envoie
# PharmA2A est une extension d'A2A : une extension de profil, au sens du texte d'A2A
# (les messages suivent les schémas du dictionnaire). Son adresse porte la version du
# dictionnaire ; un changement qui casse fait une nouvelle adresse, jamais la même.
EXTENSION = "https://pharma2a.org/ext/v1"


def marquer(artefact):
    """Un artefact qui porte un message du dictionnaire le dit, par l'adresse de l'extension."""
    if EXTENSION not in artefact.extensions:
        artefact.extensions.append(EXTENSION)
    return artefact


@lru_cache(maxsize=None)
def validateur(nom: str) -> Draft202012Validator:
    schema = json.loads((SCHEMAS / f"{nom}.json").read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=FormatChecker())


def valider(nom: str, doc) -> list[str]:
    """Les erreurs, en clair, chemin puis message. Vide = conforme."""
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '/'} : {e.message}"
            for e in validateur(nom).iter_errors(doc)]


def reference_de(doc) -> str:
    """La référence d'une commande, ou d'une métadonnée de tâche qui la porte."""
    return str(doc.get("reference") or "") if isinstance(doc, dict) else ""


def erreurs_de(nom: str, doc) -> list[str]:
    """La validation complète d'un message, la même pour le guichet et l'exécuteur : le schéma,
    puis pour une commande l'état de rayon joint (présent, même vide, il doit être conforme) et
    les id_ligne, uniques dans la commande. Vide = conforme."""
    erreurs = valider(nom, doc)
    if erreurs or nom != "commande":
        return erreurs
    rayon = doc.get("etat_de_rayon")
    if rayon is not None:
        erreurs += [f"etat_de_rayon/{e}" for e in valider("etat_de_rayon", rayon)]
        # L'état de rayon joint est celui de cette officine
        if isinstance(rayon, dict) and isinstance(rayon.get("officine"), dict) and not meme_officine(rayon, doc):
            erreurs.append("etat_de_rayon/officine : ce n'est pas l'officine de la commande")
    ids = [l.get("id_ligne") for l in doc["lignes"]]
    doublons = sorted({i for i in ids if ids.count(i) > 1})
    if doublons:
        erreurs.append("lignes : id_ligne en double : " + ", ".join(map(str, doublons)))
    # Une ligne par produit (schéma) : le même code, du même type, sur deux lignes
    vus, produits = {}, []
    for l in doc["lignes"]:
        for k in IDENTIFIANTS:
            v = str(l.get(k) or "").strip()
            if v:
                if (k, v) in vus and vus[(k, v)] != l.get("id_ligne"):
                    produits.append(f"{k} {v}")
                vus.setdefault((k, v), l.get("id_ligne"))
    if produits:
        erreurs.append("lignes : un produit par ligne, en double : " + ", ".join(sorted(set(produits))))
    return erreurs


def renormaliser(doc):
    """Un Struct protobuf ne connaît que le nombre flottant : 6 arrive en 6.0.
    On rend l'entier aux valeurs entières, sans toucher au reste."""
    if isinstance(doc, float) and doc.is_integer():
        return int(doc)
    if isinstance(doc, list):
        return [renormaliser(x) for x in doc]
    if isinstance(doc, dict):
        return {k: renormaliser(v) for k, v in doc.items()}
    return doc



def _normal(x) -> str:
    return " ".join(str(x or "").split()).lower()


def identites(doc) -> list[str]:
    """Les identités de l'officine qu'une commande porte, typées : son compte client chez ce
    fournisseur, son CIP, et son nom avec son adresse complète. Deux commandes sont de la
    même officine dès qu'une identité est commune : une répétition qui ajoute le compte
    client reste la même commande, et deux homonymes à deux adresses restent deux officines."""
    off = doc.get("officine") if isinstance(doc, dict) else None
    off = off if isinstance(off, dict) else {}
    out = []
    if off.get("numero_compte_client"):
        out.append("compte:" + _normal(off["numero_compte_client"]))
    if off.get("cip_officine"):
        out.append("cip:" + _normal(off["cip_officine"]))
    adr = off.get("adresse_livraison") if isinstance(off.get("adresse_livraison"), dict) else {}
    nom_adresse = " | ".join(_normal(x) for x in (off.get("nom"), adr.get("ligne1"), adr.get("ligne2"), adr.get("code_postal"), adr.get("ville")))
    if off.get("nom"):
        out.append("adresse:" + nom_adresse)
    return out


def cle_commande(doc) -> tuple[str, str, str]:
    """(compte, référence, nature) : le compte est la première identité de l'officine (pour le
    journal et la métadonnée), la référence et la nature font la clé. La proposition et la
    commande qui la suit partagent la référence mais pas la nature : ce sont deux tâches."""
    ids = identites(doc)
    ref = reference_de(doc).strip().upper()
    nature = str(doc.get("nature") or "commande") if isinstance(doc, dict) else "commande"
    return (ids[0] if ids else ""), ref, nature


def empreinte(doc) -> str:
    """Ce qui fait le contenu d'une commande, pour reconnaître une vraie répétition : la
    nature, les lignes (identifiants, quantité, colisage, reliquat accepté), l'adresse de
    livraison et les consignes (date exigée, reliquat accepté). La date, le mot du pharmacien,
    la page de réponse ou l'état de rayon joint changent d'un envoi à l'autre sans changer
    la commande. Une consigne changée est une autre commande : rendue comme
    une répétition, elle ne serait jamais partie."""
    # l'id_ligne n'y entre pas : les premières commandes n'en avaient pas, et c'est le produit
    # et la quantité qui font le contenu
    # la consigne de reliquat compte telle qu'elle s'applique à chaque ligne : celle de la ligne,
    # sinon celle de la commande ; « false » répété sur une ligne d'une commande déjà à « false »
    # ne change rien, et n'en fait pas une autre commande
    globale = doc.get("reliquat_accepte")
    lignes = sorted((str(l.get("cip13") or ""), str(l.get("ean13") or ""), str(l.get("ref_fournisseur") or ""),
                     str(l.get("quantite") or ""), str(l.get("colisage") or ""),
                     str(l.get("reliquat_accepte") if l.get("reliquat_accepte") is not None else globale))
                    for l in (doc.get("lignes") or []) if isinstance(l, dict))
    off = doc.get("officine") if isinstance(doc.get("officine"), dict) else {}
    adr = off.get("adresse_livraison") if isinstance(off.get("adresse_livraison"), dict) else {}
    # Les valeurs que le schéma dit équivalentes se confondent : une date non
    # exigée (false, null ou absente), un pays absent (FR par défaut) ; le reliquat garde ses
    # trois valeurs (oui, non, rien dit), qui ne disent pas la même chose
    adresse = [_normal(adr.get(k)) for k in ("ligne1", "ligne2", "code_postal", "ville")] + [_normal(adr.get("pays") or "FR")]
    consignes = [doc.get("date_livraison_exigee") is True]
    # les questions d'une proposition font son contenu, dans n'importe quel ordre : une question
    # ajoutée sous la même référence n'est pas une répétition, elle ne se perdrait pas
    consignes.append(sorted({str(q) for q in doc.get("questions") or [] if q is not None}))
    return json.dumps(["4", str(doc.get("nature") or "commande"), lignes, adresse, consignes], ensure_ascii=False)


def meme_officine(doc_a, doc_b) -> bool:
    """Une identité en commun suffit."""
    return bool(set(identites(doc_a)) & set(identites(doc_b)))


def detecter(media_type: str | None, metadata: dict | None, doc) -> str | None:
    """Quel message est-ce ? Le type de média d'abord, la métadonnée ensuite,
    la forme du document en dernier recours. None si on ne sait pas : on ne devine pas."""
    for n, m in MEDIA.items():
        if media_type and media_type.lower() == m:
            return n
    if metadata and metadata.get(CLE_METADATA) in NOMS:
        return metadata[CLE_METADATA]
    if isinstance(doc, dict):
        if "periode" in doc and "fournisseur" in doc:
            return "etat_de_rayon"
        if isinstance(doc.get("fournisseur"), dict) and "date" in doc:
            return "fiche_fournisseur"
        if reference_de(doc) and "officine" in doc and "motif" in doc and "lignes" not in doc:
            return "relance"
        if reference_de(doc) and "officine" in doc and "lignes" in doc:
            return "commande"
    return None
