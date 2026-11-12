"""Le rendu de ce qui part vers le laboratoire : un bon de commande lisible, un état de
rayon en CSV. Zéro prix, par construction : ces fonctions ne reçoivent que des
documents validés contre le dictionnaire, qui n'a pas de champ prix."""
from __future__ import annotations
import os as _os
import re
import unicodedata
try:
    from .identifiants import code, code_affiche, indexer, retrouver
except ImportError:
    # Chargé par fichier (l'aperçu avant signature de l'agent d'achat) : pas de paquet autour,
    # on charge le module voisin, bibliothèque standard seule
    import importlib.util as _il
    import pathlib as _pl
    _spec = _il.spec_from_file_location("pharma2a_identifiants", _pl.Path(__file__).with_name("identifiants.py"))
    _ids = _il.module_from_spec(_spec); _spec.loader.exec_module(_ids)
    code, code_affiche, indexer, retrouver = _ids.code, _ids.code_affiche, _ids.indexer, _ids.retrouver
from html import escape as esc


def _adresse(o: dict) -> list[str]:
    a = o.get("adresse_livraison") or {}
    lignes = [a.get("ligne1"), a.get("ligne2"), f"{a.get('code_postal', '')} {a.get('ville', '')}".strip()]
    pays = a.get("pays")
    if pays and pays != "FR":
        lignes.append(pays)
    return [x for x in lignes if x]



# ---------------------------------------------------------------------------
# La charte du mail. Un mail de
# commande est la première chose qu'un laboratoire voit de l'officine et de son agent :
# une barre fine (la marque carrée, l'officine, la référence), un titre, une ligne grise,
# des filets fins, des chiffres alignés, une seule couleur, sur le bouton. Ni carte, ni
# capitales, ni bandeau sombre. Tables et styles en ligne, parce que Gmail et Outlook ne
# lisent rien d'autre. Aucune image externe : rien à charger, rien à bloquer.
# ---------------------------------------------------------------------------
ENCRE, INDIGO, FOND, BORD, GRIS, ALERTE = "#282A30", "#5E6AD2", "#FCFCFC", "#E9E9EB", "#8A8F98", "#C2410C"
GRIS_70 = "#5C5E66"
LOGO_PIED = _os.environ.get("PHARMA2A_LOGO_PIED", "")   # une image hébergée pour le pied (facultatif), ex. le logo de l'éditeur de l'agent ; sans elle, le pied est neutre
POLICE = "font-family:-apple-system,BlinkMacSystemFont,'Inter','Segoe UI',Helvetica,Arial,sans-serif"
MONO = "font-family:'JetBrains Mono',Menlo,Consolas,monospace"


def _date_fr(d) -> str:
    """2026-10-23 -> 23/10/2026 ; tout autre texte reste tel quel."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(d or ""))
    return f"{m[3]}/{m[2]}/{m[1]}" if m else str(d or "")


def _initiale(officine: str) -> str:
    mot = re.sub(r"^(grande |la |le |les |l')", "", (officine or "").strip(), flags=re.I).strip()
    return (mot[:1] or "M").upper()


def gabarit(agent: dict | None, officine: str, surtitre: str, titre: str, chips: list[str], corps: str, pied: list[str]) -> str:
    """La page : la barre (marque, officine › surtitre, l'agent en gris), le titre, la ligne
    grise (chips), le corps, le pied."""
    nom_agent = (agent or {}).get("nom")
    via = f"via {esc(nom_agent)}, agent d'achat" if nom_agent else ""
    barre = (f'<td style="padding:12px 24px;border-bottom:1px solid {BORD};{POLICE};font-size:13px;color:{ENCRE}">'
             f'<span style="display:inline-block;width:18px;height:18px;line-height:18px;border-radius:4px;background:{ENCRE};color:#fff;'
             f'font-size:11px;font-weight:600;text-align:center;vertical-align:middle;margin-right:8px">{esc(_initiale(officine))}</span>'
             f'<span style="font-weight:500;vertical-align:middle">{esc(officine)}</span>'
             f'<span style="color:{GRIS};margin:0 8px;vertical-align:middle">&rsaquo;</span>'
             f'<span style="color:{GRIS_70};vertical-align:middle">{esc(surtitre)}</span></td>'
             f'<td align="right" style="padding:12px 24px;border-bottom:1px solid {BORD};{POLICE};font-size:12px;color:{GRIS};white-space:nowrap">{via}</td>')
    sous = " &middot; ".join(esc(c) for c in chips)
    # Un document complet qui dit son encodage : ouvert tel quel, hors enveloppe
    # MIME, le fragment s'affichait en caractères cassés. Le pharmacien doit pouvoir voir
    # le mail avant de signer, dans n'importe quel navigateur.
    return ('<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
            f'<body style="margin:0;padding:0;background:{FOND}">'
            f'<div style="background:{FOND};{POLICE};color:{ENCRE};font-size:13px;line-height:1.5">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:760px;border-collapse:collapse">'
            f'<tr>{barre}</tr>'
            f'<tr><td colspan="2" style="padding:28px 24px 0">'
            f'<div style="font-size:20px;font-weight:500;letter-spacing:-0.2px;line-height:1.3">{esc(titre)}</div>'
            + (f'<div style="font-size:13px;color:{GRIS_70};margin-top:4px">{sous}</div>' if chips else "")
            + '</td></tr>'
            f'<tr><td colspan="2" style="padding:20px 24px 28px">{corps}</td></tr>'
            f'<tr><td colspan="2" style="padding:14px 24px 32px;border-top:1px solid {BORD};font-size:12px;line-height:1.6;color:{GRIS}">'
            + "".join(f"<p style=\"margin:0 0 6px\">{esc(x)}</p>" for x in pied)
            + (f'<p style="margin:14px 0 0"><img src="{LOGO_PIED}" alt="" width="72" height="16" style="display:inline-block;vertical-align:middle;opacity:.7"></p>' if LOGO_PIED else "")
            + '</td></tr></table></div></body></html>')


def _props(lignes) -> str:
    """Des propriétés à la Linear : l'étiquette en gris à gauche, la valeur à droite, un filet."""
    rows = "".join(f'<tr><td style="padding:7px 16px 7px 0;border-bottom:1px solid {BORD};font-size:13px;color:{GRIS};white-space:nowrap;vertical-align:top;width:160px">{esc(k)}</td>'
                   f'<td style="padding:7px 0;border-bottom:1px solid {BORD};font-size:13px;color:{ENCRE}">{v}</td></tr>' for k, v in lignes if v)
    return f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin:0">{rows}</table>'


def _bloc_officine(o: dict) -> str:
    lignes = [("Compte client", o.get("numero_compte_client")), ("CIP officine", o.get("cip_officine")),
              ("Téléphone", o.get("telephone")), ("Livraison", ", ".join(_adresse(o)))]
    return _props([(k, esc(str(v))) for k, v in lignes if v])


def _tableau(entetes: list[tuple[str, str]], lignes_html: list[str]) -> str:
    """entetes : (libellé, 'left'|'right'). Tête grise, filets fins, chiffres à droite."""
    th = "".join(f'<th style="text-align:{al};padding:0 {"0" if i == len(entetes) - 1 else "12px"} 8px 0;font-size:12px;font-weight:500;color:{GRIS};border-bottom:1px solid {BORD};white-space:nowrap">{esc(t)}</th>'
                 for i, (t, al) in enumerate(entetes))
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin:0 0 12px">'
            f'<tr>{th}</tr>{"".join(lignes_html)}</table>')


def _td(contenu: str, al: str = "left", style: str = "", dernier: bool = False) -> str:
    return (f'<td style="padding:9px {"0" if dernier else "12px"} 9px 0;border-bottom:1px solid {BORD};font-size:13px;color:{ENCRE};'
            f'text-align:{al};vertical-align:middle;{style}">{contenu}</td>')


def _liste(items) -> str:
    """Une ligne par élément, un filet entre deux, sans puce."""
    return "".join(f'<div style="padding:7px 0;border-bottom:1px solid {BORD};font-size:13px">{esc(x[:1].upper() + x[1:])}</div>' for x in items)


def _h2(t: str) -> str:
    return f'<div style="font-size:13px;font-weight:500;margin:24px 0 0;padding-bottom:8px;border-bottom:1px solid {BORD}">{esc(t)}</div>'


QUESTIONS = {
    "franco": "votre franco de port",
    "colisage": "le colisage des références ci-dessous",
    "delai_livraison": "votre délai de livraison",
    "catalogue": "votre tarif à jour avec les colisages (Excel de préférence, sinon PDF)",
}


def _voix(doc: dict, objet_du_mail: str) -> tuple[list[str], list[str]]:
    """L'ouverture et la signature du mail. Si l'agent de l'officine se présente, c'est lui
    qui parle, au nom de l'officine et du pharmacien qui a validé ; sinon, le ton neutre."""
    # Le commercial n'est pas l'acheteur (un pharmacien : « on ne va pas le convaincre, il
    # prend la commande, il nous renvoie les détails qui manquent »). Une phrase, le bon,
    # la signature. Rien d'autre.
    a = doc.get("agent") or {}
    o = doc["officine"]
    signature = [f"{a['nom']}, agent d'achat de l'officine {o['nom']}"] if a.get("nom") else [o["nom"]]
    if doc.get("nature") == "proposition":
        # La proposition : la commande AVANT la commande, pour que le laboratoire la
        # confirme et donne ce qui manque. Elle ne s'expédie pas.
        return ["Bonjour,", f"Voici la commande que l'officine {o['nom']} prépare chez vous ({objet_du_mail}). "
                            "Rien à expédier : la commande définitive suivra."], signature
    qui = f", validée par {a['signataire']}" if a.get("signataire") else ""
    return ["Bonjour,", f"Voici {objet_du_mail} de l'officine {o['nom']}{qui}."], signature


def _contexte_rayon(doc: dict) -> dict:
    """cip13 -> ligne de l'état de rayon joint à la commande (stock, ventes), s'il y en a un."""
    r = doc.get("etat_de_rayon") or {}
    return indexer(r.get("lignes") or [])


def _franco(doc: dict) -> str | None:
    """« Franco de port : 300 € HT, atteint par cette commande. » Seulement quand l'agent de
    l'officine l'a vérifié (franco_ht du document) ; jamais le montant de la commande."""
    f = doc.get("franco_ht")
    if not f:
        return None
    montant = f"{int(f)}" if float(f) == int(f) else f"{float(f):.2f}".replace(".", ",")
    return f"Franco de port : {montant} € HT, atteint par cette commande."


def _accord(rayon: dict | None) -> str | None:
    """Ce que le pharmacien autorise le fournisseur à faire de son état de rayon, en clair."""
    a = (rayon or {}).get("accord")
    if not isinstance(a, dict):
        return None
    usage = ("Ce partage sert uniquement à mieux livrer l'officine." if a.get("usage") == "livraison"
             else "Ce partage peut aussi servir à vos prévisions, sans que ces données soient cédées.")
    jusqu = (f" Il vaut jusqu'au {_date_fr(a['valable_jusqu_au'])}." if a.get("valable_jusqu_au")
             else " Il vaut jusqu'à ce que le pharmacien le retire.")
    return usage + jusqu


def _consignes(doc: dict) -> list[str]:
    """Ce que l'officine attend de la réponse : une date ferme, et ce qu'elle dit des reliquats."""
    out = []
    if doc.get("date_livraison_exigee") is True:
        out.append("Merci d'indiquer une date de livraison ferme.")
    if doc.get("reliquat_accepte") is True:
        out.append("Reliquats acceptés : ce qui ne part pas tout de suite peut suivre sans nouvelle commande.")
    elif doc.get("reliquat_accepte") is False:
        out.append("Pas de reliquat : ce qui ne part pas tout de suite est annulé, l'officine recommandera.")
    return out


def _marque_reliquat(doc: dict, l: dict) -> str:
    """La ligne qui dit autre chose que la commande sur le reliquat le montre."""
    v, defaut = l.get("reliquat_accepte"), doc.get("reliquat_accepte")
    if v is None or v == defaut:
        return ""
    return " (reliquat accepté)" if v else " (sans reliquat)"


def commande(doc: dict, remplace=None) -> tuple[str, str, str]:
    """(objet, texte, html) du mail d'une commande. Si l'état de rayon est joint, chaque ligne
    commandée montre son stock et ses ventes : le laboratoire voit pourquoi on commande.
    remplace : « fermee » (ou True), un envoi précédent sous la même référence a été fermé
    par l'officine, le mail annule et remplace ; « proposition », la commande ferme suit la
    proposition envoyée sous la même référence ; « incertain », un envoi précédent a échoué
    sans qu'on sache s'il est parti, le mail dit que s'il est arrivé, celui-ci le remplace.
    Dans les deux cas, le fournisseur ne compte pas deux commandes."""
    if remplace is True:
        remplace = "fermee"
    o = doc["officine"]
    ref = doc["reference"]
    ctx = _contexte_rayon(doc)
    prop = doc.get("nature") == "proposition"
    questions = [QUESTIONS[q] for q in (doc.get("questions") or []) if q in QUESTIONS]
    objet = (f"Proposition de commande {ref} - {o['nom']} : merci de confirmer" if prop
             else f"Commande {ref} - {o['nom']}")
    if remplace == "fermee":
        objet += " (annule et remplace)"
    unites = sum(int(l["quantite"]) for l in doc["lignes"])
    jour = _date_fr(doc["date"])
    ouverture, signature = _voix(doc, f"référence {ref} du {jour}" if prop else f"la commande {ref} du {jour}")
    if remplace == "fermee":
        ouverture.append(f"Ce message annule et remplace l'envoi précédent sous la référence {ref} : "
                         "seule la commande ci-dessous est à prendre en compte.")
    elif remplace == "proposition":
        ouverture.append(f"Cette commande fait suite à la proposition envoyée sous la référence {ref}, et la remplace : "
                         "c'est elle qui est à expédier.")
    elif remplace:
        ouverture.append(f"Si vous avez déjà reçu un message sous la référence {ref}, celui-ci le remplace : "
                         "seule la commande ci-dessous est à prendre en compte.")
    tete = [f"{'Proposition' if prop else 'Commande'} {ref} du {jour}"]
    if o.get("numero_compte_client"):
        tete.append(f"Compte client : {o['numero_compte_client']}")
    if o.get("cip_officine"):
        tete.append(f"CIP officine : {o['cip_officine']}")
    if o.get("telephone"):
        tete.append(f"Téléphone : {o['telephone']}")
    tete.append("Livraison : " + ", ".join(_adresse(o)))
    # Le colisage et le franco dans le bon (les laboratoires les demandaient : « cela
    # évite au commercial de faire un retour en disant que telle ligne c'est par tant
    # d'unités », « idem le franco, cela évite de demander d'ajouter des lignes »). Le bon
    # montre ce qu'il sait ; une case vide dit que l'officine ne le sait pas encore.
    franco = _franco(doc)
    if franco:
        tete.append(franco)
    avec_col = any(l.get("colisage") for l in doc["lignes"])
    def v(x): return "" if x is None else str(x)
    def col(l): return f"{v(l.get('colisage')):>5}  " if avec_col else ""
    if ctx:
        lignes_txt = [f"{int(l['quantite']):>5}  {v((retrouver(ctx, l) or {}).get('ventes_3_mois')):>6}  {v((retrouver(ctx, l) or {}).get('stock')):>5}  {col(l)}{code_affiche(l)}  {l['libelle']}{_marque_reliquat(doc, l)}"
                      for l in doc["lignes"]]
        entete_txt = "  Qté  Ventes  Stock  " + ("Colis  " if avec_col else "") + "Code           Désignation"
    else:
        lignes_txt = [f"{int(l['quantite']):>5}  {col(l)}{code_affiche(l)}  {l['libelle']}{_marque_reliquat(doc, l)}" for l in doc["lignes"]]
        entete_txt = "  Qté  " + ("Colis  " if avec_col else "") + "Code           Désignation"
    pied = [f"En réponse, avec la référence {ref} : votre numéro de commande, la date de livraison prévue, "
            "et les lignes qui ne partent pas telles quelles."]
    if prop:
        pied = [f"En réponse, avec la référence {ref} : les lignes qui ne partiraient pas telles quelles."]
    # La page de réponse : un lien propre à cette commande, où le laboratoire corrige,
    # dépose son tarif et valide, sans écrire. Le mail reste la voie normale.
    page = doc.get("page_reponse")
    if page:
        # Une commande ferme se confirme, elle ne se refait pas (un commercial, après
        # sa proposition corrigée, recevait le même « répondre sans écrire » et ne savait plus
        # quelle version traiter)
        pied = ([f"Pour répondre sans écrire : {page}", "Ou répondez simplement à ce message."] if prop else
                [f"Pour confirmer la commande et donner la date de livraison, sans écrire : {page}",
                 "Ou répondez simplement à ce message."])
    bloc_questions = ("Il me manque : " + " ; ".join(questions) + ".") if questions else None
    rayon = doc.get("etat_de_rayon")
    note_rayon = None
    if rayon:
        note_rayon = (f"Stock et ventes 3 mois du rayon au {_date_fr(rayon.get('date', doc['date']))}. "
                      f"L'état de rayon complet ({len(rayon.get('lignes') or [])} références) est joint en Excel."
                      + (" " + _accord(rayon) if _accord(rayon) else ""))
    consignes = [] if prop else _consignes(doc)
    texte = "\n".join(ouverture) + "\n\n" + (bloc_questions + "\n\n" if bloc_questions else "") \
            + "\n".join(tete) + "\n\n" + "\n".join([entete_txt] + lignes_txt) \
            + f"\n\n{len(doc['lignes'])} lignes, {unites} unités.\n" \
            + ("\n" + "\n".join(consignes) + "\n" if consignes else "") \
            + (f"\n{note_rayon}\n" if note_rayon else "") \
            + (f"\nMessage du pharmacien : {doc['message']}\n" if doc.get("message") else "") \
            + "\n" + "\n".join(pied) + "\n\n" + "\n".join(signature) + "\n"
    # Le tableau, dans l'ordre de la page : le produit, puis Commandé, Ventes
    # 3 mois, Stock, Colisage, chiffres à droite. Le stock à zéro n'est pas une alerte.
    rows = []
    for l in doc["lignes"]:
        c = retrouver(ctx, l) or {}
        cells = [_td(f'{esc(l["libelle"] + _marque_reliquat(doc, l))}<div style="font-size:11px;color:{GRIS};{MONO};margin-top:1px;line-height:1.2">{esc(code_affiche(l))}</div>')]
        cells.append(_td(f'<span style="font-weight:500">{int(l["quantite"])}</span>', "right", dernier=not ctx and not avec_col))
        if ctx:
            cells += [_td(v(c.get("ventes_3_mois")), "right", f"color:{GRIS_70}"), _td(v(c.get("stock")), "right", f"color:{GRIS_70}", dernier=not avec_col)]
        if avec_col:
            cells.append(_td(str(int(l["colisage"])) if l.get("colisage") else "", "right", dernier=True))
        rows.append("<tr>" + "".join(cells) + "</tr>")
    entetes = [("Produit", "left"), ("Commandé", "right")] + ([("Ventes 3 mois", "right"), ("Stock", "right")] if ctx else []) + ([("Colisage", "right")] if avec_col else [])
    chips = [f"{len(doc['lignes'])} lignes", f"{unites} unités"] + (["franco atteint"] if franco else [])
    corps = ("".join(f'<p style="margin:0 0 10px;font-size:14px;line-height:1.55;max-width:560px">{esc(x)}</p>' for x in ouverture)
             + (_h2("Il me manque") + _liste(questions) if questions else "")
             + _h2("L'officine") + _bloc_officine(o)
             + (f'<p style="margin:10px 0 0;font-size:13px;color:{GRIS_70}">{esc(franco)}</p>' if franco else "")
             + _h2("La commande") + '<div style="height:4px"></div>' + _tableau(entetes, rows)
             + "".join(f'<p style="font-size:13.5px;line-height:1.5;margin:0 0 8px;max-width:560px">{esc(x)}</p>' for x in consignes)
             + (f'<p style="font-size:12.5px;color:{GRIS};line-height:1.5;margin:0 0 10px;max-width:560px">{esc(note_rayon)}</p>' if note_rayon else "")
             + (_h2("Message du pharmacien") + f'<p style="font-size:14px;margin:10px 0 0;line-height:1.55;max-width:560px">{esc(doc["message"])}</p>' if doc.get("message") else "")
             + (f'<p style="margin:24px 0 8px"><a href="{esc(page)}" style="display:inline-block;padding:8px 14px;background:{INDIGO};color:#fff;text-decoration:none;border-radius:6px;font-size:13px;font-weight:500">{"Répondre sur la page" if prop else "Confirmer la commande"}</a></p>'
                + (f'<p style="font-size:12.5px;color:{GRIS};margin:0 0 12px">Le franco, le délai, les colisages, ce qui ne part pas, votre tarif. Ou répondez simplement à ce message.</p>' if prop else
                   f'<p style="font-size:12.5px;color:{GRIS};margin:0 0 12px">Votre numéro de commande, la date de livraison, et seulement ce qui ne part pas tel quel. Ou répondez simplement à ce message.</p>') if page else
                "".join(f'<p style="font-size:13px;line-height:1.55;margin:16px 0 8px;max-width:560px">{esc(x)}</p>' for x in pied))
             + f'<p style="margin:20px 0 0;font-size:13px;line-height:1.55;color:{GRIS_70}">' + "<br>".join(esc(x) for x in signature) + "</p>")
    html = gabarit(doc.get("agent"), o["nom"], ref,
                   f"Proposition de commande {ref}" if prop else f"Commande {ref}", chips, corps,
                   ["Transmis sur PharmA2A, le protocole ouvert de la commande directe entre officine et fournisseur. Aucun montant n'y circule."])
    return objet, texte, html


def _texte_seulement(cellules):
    """Un libellé ou une référence qui commence par « = » n'est pas une formule : du texte."""
    for c in cellules:
        if isinstance(c.value, str):
            c.data_type = "s"


COLONNES_RAYON = ("cip13", "libelle", "stock", "ventes_3_mois", "couverture_jours", "mois_a_zero_3_mois")
TITRES_RAYON = ("Code", "Désignation", "Stock", "Ventes 3 mois", "Couverture (jours)", "Mois à zéro / 3")


def _tri_rayon(lignes):
    """À zéro d'abord, puis par ventes décroissantes : ce que le laboratoire regarde en premier."""
    return sorted(lignes, key=lambda l: (0 if (l.get("stock") or 0) <= 0 else 1, -(l.get("ventes_3_mois") or 0), l.get("libelle") or ""))


def etat_de_rayon_xlsx(doc: dict, commande: dict | None = None) -> bytes:
    """Le fichier Excel. Avec une commande : onglet « Commande » (prêt à saisir) puis onglet
    « État de rayon » avec la colonne « Commandé ». Sans : l'état de rayon seul. En-tête figé,
    filtres, colonnes à la bonne largeur, lignes à zéro surlignées."""
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = Workbook(); wb.remove(wb.active)
    o = doc["officine"]; p = doc["periode"]
    noir = PatternFill("solid", fgColor="14142B"); zero = PatternFill("solid", fgColor="FDE2E1"); cmd = PatternFill("solid", fgColor="E8E9FB")

    def feuille(titre, sous_titre, colonnes, largeurs):
        ws = wb.create_sheet(titre)
        ws["A1"] = sous_titre; ws["A1"].font = Font(bold=True, size=13)
        ws["A2"] = f"Ventes et mois à zéro comptés du {p['du']} au {p['au']}. Stock du matin. Aucun prix, aucune marge."
        ws["A2"].font = Font(italic=True, color="666666")
        ws.append([]); ws.append(list(colonnes)); tete = ws.max_row
        for c in ws[tete]:
            c.font = Font(bold=True, color="FFFFFF"); c.fill = noir; c.alignment = Alignment(horizontal="center", vertical="center")
        for i, largeur in enumerate(largeurs, start=1):
            ws.column_dimensions[get_column_letter(i)].width = largeur
        return ws, tete

    def finir(ws, tete, ncol, col_cip):
        col_libelle = col_cip + 1
        for row in ws.iter_rows(min_row=tete + 1, max_row=ws.max_row):
            for c in row:
                if c.column not in (col_cip, col_libelle):
                    c.alignment = Alignment(horizontal="center")
            row[col_cip - 1].number_format = "@"
        ws.freeze_panes = ws.cell(row=tete + 1, column=1)
        ws.auto_filter.ref = f"A{tete}:{get_column_letter(ncol)}{ws.max_row}"

    quantites = indexer((commande or {}).get("lignes") or [])
    rayon_par_cip = indexer(doc["lignes"])
    if commande:
        ws, tete = feuille("Commande", f"Commande {commande['reference']} du {commande['date']} · {o['nom']}",
                           ("Qté commandée", "Colisage", "Code", "Désignation", "Stock", "Ventes 3 mois", "Couverture (jours)"), (14, 10, 16, 46, 9, 14, 18))
        franco = _franco(commande)
        if franco:
            ws["A2"] = franco + " " + ws["A2"].value
        for l in commande["lignes"]:
            r = retrouver(rayon_par_cip, l) or {}
            ws.append([int(l["quantite"]), l.get("colisage"), code_affiche(l), l["libelle"], r.get("stock"), r.get("ventes_3_mois"), r.get("couverture_jours")])
            _texte_seulement(ws[ws.max_row])
            ws.cell(row=ws.max_row, column=1).font = Font(bold=True)
        finir(ws, tete, 7, 3)
    ws, tete = feuille("État de rayon", f"État de rayon {doc['fournisseur']} · {o['nom']} · relevé du {doc['date']}",
                       (("Commandé",) if commande else ()) + TITRES_RAYON, ((11,) if commande else ()) + (16, 46, 9, 14, 18, 15))
    for l in _tri_rayon(doc["lignes"]):
        ligne = ([(retrouver(quantites, l) or {}).get("quantite") or None] if commande else []) + [code_affiche(l), l.get("libelle"), l.get("stock"), l.get("ventes_3_mois"), l.get("couverture_jours"), l.get("mois_a_zero_3_mois")]
        ws.append(ligne)
        _texte_seulement(ws[ws.max_row])
        if (l.get("stock") or 0) <= 0:
            for c in ws[ws.max_row]: c.fill = zero
        if commande and (retrouver(quantites, l) or {}).get("quantite"):
            ws.cell(row=ws.max_row, column=1).font = Font(bold=True); ws.cell(row=ws.max_row, column=1).fill = cmd
    finir(ws, tete, 7 if commande else 6, 2 if commande else 1)
    buf = io.BytesIO(); wb.save(buf); return buf.getvalue()


def etat_de_rayon(doc: dict) -> tuple[str, str, str, str]:
    """(objet, texte, html, nom_de_fichier) ; le tableau est DANS le mail, l'Excel en pièce jointe."""
    o = doc["officine"]
    objet = f"État de rayon {doc['fournisseur']} - {o['nom']} - {doc['date']}"
    p = doc["periode"]
    lignes = _tri_rayon(doc["lignes"])
    a_zero = sum(1 for l in lignes if (l.get("stock") or 0) <= 0)
    ouverture, signature = _voix(doc, f"le partage de son état de rayon au {doc['date']}")
    tete = ouverture + [
            f"Voici l'état de rayon de {o['nom']} sur vos références, relevé du {doc['date']} : "
            f"{len(lignes)} références, dont {a_zero} à zéro. Ventes et mois à zéro comptés du {p['du']} au {p['au']}, stock du matin.",
            "Le même tableau est joint en Excel, trié et filtrable."]
    if o.get("numero_compte_client"):
        tete.append(f"Compte client : {o['numero_compte_client']}")
    pied = ["Ce partage est décidé par le pharmacien, qui peut l'arrêter à tout moment. Il ne contient ni prix ni marge."
            + (" " + _accord(doc) if _accord(doc) else "")] + signature
    def v(x): return "" if x is None else str(x)
    texte = "\n".join(tete) + "\n\n" + "\n".join(
        f"{v(l.get('stock')):>5}  {v(l.get('ventes_3_mois')):>5}  {v(l.get('couverture_jours')):>5}  {v(l.get('mois_a_zero_3_mois')):>3}  {code_affiche(l)}  {l['libelle']}"
        for l in lignes) + "\n\n(stock · ventes 3 mois · couverture jours · mois à zéro · CIP · désignation)\n\n" + "\n".join(pied) + "\n"
    rows = []
    for l in lignes:
        rows.append("<tr>" + _td(f'{esc(l["libelle"])}<div style="font-size:11px;color:{GRIS};{MONO};margin-top:1px;line-height:1.2">{esc(code_affiche(l))}</div>')
                    + _td(f'<span style="font-weight:500">{v(l.get("stock"))}</span>', "right")
                    + _td(v(l.get("ventes_3_mois")), "right", f"color:{GRIS_70}") + _td(v(l.get("couverture_jours")), "right", f"color:{GRIS_70}") + _td(v(l.get("mois_a_zero_3_mois")), "right", f"color:{GRIS_70}", dernier=True) + "</tr>")
    corps = ("".join(f'<p style="margin:0 0 10px;font-size:14.5px;line-height:1.55">{esc(x)}</p>' for x in tete)
             + _tableau([("Produit", "left"), ("Stock", "right"), ("Ventes 3 mois", "right"), ("Couverture (j)", "right"), ("Mois à zéro / 3", "right")], rows)
             + "".join(f'<p style="font-size:13px;line-height:1.55;margin:0 0 8px">{esc(x)}</p>' for x in pied))
    html = gabarit(doc.get("agent"), o["nom"], "État de rayon", f"Vos références chez {o['nom']}",
                   [f"{len(lignes)} références", f"{a_zero} à zéro", f"relevé du {doc['date']}", "Excel joint"], corps,
                   ["Ce partage est décidé par le pharmacien, qui peut l'arrêter à tout moment. Il ne contient ni prix ni marge.",
                    "Transmis sur PharmA2A, le protocole ouvert de la commande directe."
                    + (f" {doc['agent']['nom']} est l'agent d'achat de l'officine." if (doc.get("agent") or {}).get("nom") else "")])
    # un nom de fichier sans espace ni accent, quel que soit le nom du laboratoire
    labo = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", doc["fournisseur"]).encode("ascii", "ignore").decode().lower()).strip("-")
    nom = f"etat-de-rayon-{labo}-{doc['date']}.xlsx"
    return objet, texte, html, nom


def relance(doc: dict, commande: dict | None = None) -> tuple[str, str, str]:
    """(objet, texte, html) du mail de relance. Il part dans le fil de la commande, rappelle
    la référence, la date annoncée et les lignes commandées, et demande où en est la commande.
    Une relance ne commande rien : elle ne change ni les produits ni les quantités. Le ton est
    celui d'un fait, sans reproche et sans calcul."""
    o = doc["officine"]
    ref = doc["reference"]
    a = doc.get("agent") or {}
    signature = [f"{a['nom']}, agent d'achat de l'officine {o['nom']}"] if a.get("nom") else [o["nom"]]
    jour = _date_fr(doc["date"])
    quand = f" du {_date_fr(commande['date'])}" if (commande or {}).get("date") else ""
    if doc["motif"] == "livraison_en_retard":
        annonce = f", annoncée pour le {_date_fr(doc['date_livraison_annoncee'])}," if doc.get("date_livraison_annoncee") else ""
        constat = f"La livraison de la commande {ref}{quand}{annonce} n'est pas arrivée à ce jour ({jour})."
        demande = "Pouvez-vous nous dire où elle en est, et nous donner la nouvelle date de livraison ?"
    else:
        constat = f"La commande {ref}{quand} est restée sans réponse à ce jour ({jour})."
        demande = "Pouvez-vous nous confirmer sa prise en compte, avec votre numéro de commande et la date de livraison prévue ?"
    ouverture = ["Bonjour,", constat, demande,
                 "Ce message ne passe pas de nouvelle commande : il porte sur celle-ci, telle qu'elle a été envoyée."]
    objet = f"Relance : commande {ref} - {o['nom']}"
    lignes = [l for l in (commande or {}).get("lignes") or [] if isinstance(l, dict)]
    lignes_txt = [f"{int(l['quantite']):>5}  {code_affiche(l)}  {l['libelle']}" for l in lignes]
    texte = "\n".join(ouverture) + "\n\n" \
            + ("\n".join(["  Qté  Code           Désignation"] + lignes_txt) + "\n\n" if lignes else "") \
            + (f"Message du pharmacien : {doc['message']}\n\n" if doc.get("message") else "") \
            + "\n".join(signature) + "\n"
    style_code = f"font-size:11px;color:{GRIS};{MONO};margin-top:1px"
    rows = ["<tr>" + _td(esc(l["libelle"]) + '<div style="' + style_code + '">' + esc(code_affiche(l)) + "</div>")
            + _td(str(int(l["quantite"])), "right", dernier=True) + "</tr>" for l in lignes]
    corps = ("".join(f'<p style="margin:0 0 10px;font-size:14px;line-height:1.55;max-width:560px">{esc(x)}</p>' for x in ouverture)
             + (_h2("La commande") + '<div style="height:4px"></div>' + _tableau([("Produit", "left"), ("Commandé", "right")], rows) if rows else "")
             + (_h2("Message du pharmacien") + f'<p style="font-size:14px;margin:10px 0 0;line-height:1.55;max-width:560px">{esc(doc["message"])}</p>' if doc.get("message") else "")
             + f'<p style="margin:20px 0 0;font-size:13px;line-height:1.55;color:{GRIS_70}">' + "<br>".join(esc(x) for x in signature) + "</p>")
    chips = ["livraison en retard" if doc["motif"] == "livraison_en_retard" else "sans réponse"] + ([f"{len(lignes)} lignes"] if lignes else [])
    html = gabarit(doc.get("agent"), o["nom"], ref, f"Relance de la commande {ref}", chips, corps,
                   ["Transmis sur PharmA2A, le protocole ouvert de la commande directe entre officine et fournisseur. Aucun montant n'y circule."])
    return objet, texte, html
