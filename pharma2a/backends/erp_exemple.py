"""Un canal ERP de référence, à copier en backends/erp.py et à adapter. Il échange des
fichiers JSON par deux dossiers, comme beaucoup d'ERP le font déjà :

  PHARMA2A_ERP_SORTIE  l'agent y dépose chaque document validé (commande, état de rayon,
                       relance) : <message>-<message_id>.json, un fichier par envoi, que deux
                       officines aux mêmes références n'écrasent jamais. Le fichier porte
                       {"message", "message_id", "reference", "document"}.
  PHARMA2A_ERP_ENTREE  l'ERP y dépose chaque accusé de réception, n'importe quel nom en .json :
                       {"en_reponse_a": "<message_id de l'envoi>", "accuse": {...}}, ou
                       l'accusé seul. L'accusé est le document du dictionnaire, avec sa
                       `reference` et l'`id_ligne` de chaque ligne. `en_reponse_a` dit à
                       quel envoi il répond : sans lui, deux commandes de même référence
                       restent ambiguës et l'accusé attend. L'agent déplace le fichier dans
                       traites/ une fois lu, dans rejetes/ s'il n'est pas un objet JSON.

Un ERP qui a une API remplace les trois méthodes par ses appels ; le reste ne change pas."""
from __future__ import annotations
import hashlib
import json
import logging
import os
import pathlib
import re
import uuid
from datetime import datetime, timezone
from . import Reponse

log = logging.getLogger("pharma2a.erp")


class Backend:
    origine = "erp"

    def __init__(self, config):
        self.c = config
        self.sortie = pathlib.Path(os.environ.get("PHARMA2A_ERP_SORTIE") or "erp/sortie")
        self.entree = pathlib.Path(os.environ.get("PHARMA2A_ERP_ENTREE") or "erp/entree")

    def transmettre(self, message: str, doc: dict, reference: str, message_id: str | None = None) -> str:
        """Le document validé part dans l'ERP. Rend l'identifiant que l'ERP lui donne (ici, le
        nom du fichier). Le nom tient à l'envoi, jamais à la seule référence."""
        self.sortie.mkdir(parents=True, exist_ok=True)
        envoi = message_id or f"<{uuid.uuid4()}@pharma2a>"
        nom = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{message}-{envoi}").strip("-") + ".json"
        tmp = self.sortie / ("." + nom + ".tmp")
        tmp.write_text(json.dumps({"message": message, "message_id": envoi, "reference": reference, "document": doc},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.sortie / nom)          # l'ERP ne voit jamais un fichier à moitié écrit
        return nom

    def lire_reponses(self, depuis: datetime | None = None) -> list[Reponse]:
        """Les accusés de l'entrée, et ceux qu'une passe précédente a gardés en réserve, lus dans
        l'ordre où l'ERP les a déposés : un accusé plus ancien, resté en réserve, passe avant
        celui qui lui a succédé, et ne rétablit jamais une date déjà corrigée."""
        self._lus = {}
        out = []
        for f in self._candidats():
            try:
                octets = f.read_bytes()
                brut = json.loads(octets.decode("utf-8"))
            except Exception:
                continue                      # un fichier en cours d'écriture : relu à la passe suivante
            enveloppe = isinstance(brut, dict) and isinstance(brut.get("accuse"), dict)
            accuse = brut["accuse"] if enveloppe else brut
            if not isinstance(accuse, dict):
                self._rejeter(f)              # un fichier qui n'est pas un objet ne bloque pas les autres
                continue
            cite = str(brut.get("en_reponse_a") or "") if enveloppe else ""
            # L'identité d'une réponse est son nom et son contenu : un ERP qui
            # réutilise « retour.json » pour un nouvel accusé n'est pas pris pour une répétition
            ident = f"{f.name}#{_empreinte(octets)}"
            self._lus[ident] = f
            out.append(Reponse(identifiant=ident, objet="", texte="", recu_le=datetime.now(timezone.utc),
                               reference=str(accuse.get("reference") or "") or None, reference_explicite=True,
                               accuse=accuse, en_reponse_a=[cite] if cite else []))
        return out

    def _candidats(self) -> list[pathlib.Path]:
        fichiers = sorted(self.entree.glob("*.json")) if self.entree.exists() else []
        if self._reserve.exists():
            fichiers += [f for case in self._reserve.iterdir() if case.is_dir() for f in case.glob("*.json")]
        def depose(f):
            try:
                return f.stat().st_mtime_ns
            except OSError:
                return 0
        # à horodatage égal, la réserve passe d'abord : elle a été déposée avant ce qui l'a remplacée
        return sorted(fichiers, key=lambda f: (depose(f), 0 if self._reserve in f.parents else 1, f.name))

    def _reserver(self, f: pathlib.Path) -> pathlib.Path | None:
        """Déplace atomiquement un fichier de l'entrée dans sa case de réserve, où l'ERP ne
        l'atteint plus ; la case garde son nom d'origine. None s'il n'est plus là."""
        case = self._reserve / uuid.uuid4().hex
        case.mkdir(parents=True)
        pris = case / f.name
        try:
            f.replace(pris)
            return pris
        except FileNotFoundError:
            case.rmdir()
            return None
        except OSError as e:
            case.rmdir()
            log.warning("accusé ERP %s : réservation impossible (%s), relu à la passe suivante", f.name, e)
            return None

    def _rejeter(self, f: pathlib.Path) -> None:
        """Le rejet porte sur la version réservée, relue : si l'ERP a remplacé le fichier par un
        accusé valide entre-temps, c'est cet accusé qui reste à lire, rien ne part aux rejets."""
        pris = f if self._reserve in f.parents else self._reserver(f)
        if pris is None:
            return
        try:
            brut = json.loads(pris.read_bytes().decode("utf-8"))
        except Exception:
            return                            # illisible : peut-être en cours d'écriture, il attend en réserve
        accuse = brut["accuse"] if isinstance(brut, dict) and isinstance(brut.get("accuse"), dict) else brut
        if isinstance(accuse, dict):
            return                            # devenu un accusé : il attend en réserve, lu à la passe suivante
        log.warning("accusé ERP %s : la racine n'est pas un objet JSON, rangé dans rejetes/", pris.name)
        (self.entree / "rejetes").mkdir(exist_ok=True)
        dest = self.entree / "rejetes" / pris.name
        if dest.exists():
            dest = dest.with_name(_nom_borne(pris.stem, f"-{uuid.uuid4().hex[:8]}{pris.suffix}"))
        pris.replace(dest)
        _vider(pris.parent)

    def acquitter(self, identifiants: list[str]) -> None:
        """Seulement le fichier lu. Un fichier de l'entrée est d'abord réservé par un déplacement
        atomique, puis vérifié : un nouvel accusé que l'ERP dépose sous le même nom pendant ce
        temps n'est jamais archivé à la place de l'ancien, il reste en réserve et se lit à la
        passe suivante, dans son ordre. Un fichier qui résiste n'empêche pas les suivants."""
        traites = self.entree / "traites"
        traites.mkdir(parents=True, exist_ok=True)
        lus = getattr(self, "_lus", {})
        for i in identifiants:
            nom, _, signe = i.rpartition("#")
            f = lus.get(i) or self.entree / nom
            pris = f if self._reserve in f.parents else self._reserver(f)
            if pris is None or not pris.exists():
                continue                      # déjà acquitté, ou retiré par l'ERP
            try:
                if _empreinte(pris.read_bytes()) == signe:
                    pris.replace(traites / _nom_borne(pathlib.Path(nom).stem, f"-{signe}{pathlib.Path(nom).suffix}"))
                    _vider(pris.parent)
                # sinon ce n'est pas celui qu'on a lu : il reste en réserve, à lire
            except OSError as e:
                log.warning("accusé ERP %s : archivage impossible (%s), repris à la passe suivante", nom, e)

    @property
    def _reserve(self) -> pathlib.Path:
        return self.entree / ".reserve"


def _vider(case: pathlib.Path) -> None:
    """Retire une case de réserve vide."""
    try:
        if case.name != ".reserve" and not any(case.iterdir()):
            case.rmdir()
    except OSError:
        pass


def _nom_borne(base: str, fin: str, limite: int = 255) -> str:
    """base + fin en au plus `limite` octets (la limite des noms de fichier), sans couper un caractère."""
    place = limite - len(fin.encode("utf-8"))
    return base.encode("utf-8")[:max(place, 1)].decode("utf-8", "ignore") + fin


def _empreinte(octets: bytes) -> str:
    return hashlib.sha256(octets).hexdigest()[:16]
