"""Le registre des envois : une ligne par commande transmise, dans une petite base SQLite à côté
du magasin des tâches (<base>.envois.sqlite, en mémoire sans base).

C'est la base elle-même qui refuse le doublon (revue du 4 novembre, idée reprise du pont de
référence) : pour une officine prouvée par son jeton, la même référence et la même nature ne
peuvent avoir qu'un seul envoi vivant (index unique partiel). La réservation se fait juste avant
d'envoyer ; si elle échoue, rien ne part. Un envoi se ferme quand l'officine ferme sa tâche :
la référence peut alors repartir, et le mail dit qu'il annule et remplace le précédent.

Sous le jeton unique (une seule officine, officine vide), le registre sert seulement à
retrouver les tâches d'une clé sans parcourir le magasin page par page."""
from __future__ import annotations
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS envois (
    agent TEXT NOT NULL,
    officine TEXT NOT NULL,
    reference TEXT NOT NULL,
    nature TEXT NOT NULL,
    tache TEXT NOT NULL UNIQUE,
    etat TEXT NOT NULL DEFAULT 'vivant',
    destinataires TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS un_envoi_vivant
    ON envois(agent, officine, reference, nature) WHERE officine <> '' AND etat = 'vivant';
CREATE INDEX IF NOT EXISTS envois_cle ON envois(agent, officine, reference, nature);
"""


class Index:
    def __init__(self, base: str | None = None):
        self._verrou = threading.Lock()
        self._db = sqlite3.connect(base + ".envois.sqlite" if base else ":memory:", check_same_thread=False,
                                   isolation_level=None)
        self._db.execute("PRAGMA journal_mode = WAL" if base else "PRAGMA journal_mode = MEMORY")
        self._db.executescript(SCHEMA)
        if "destinataires" not in {r[1] for r in self._db.execute("PRAGMA table_info(envois)")}:
            self._db.execute("ALTER TABLE envois ADD COLUMN destinataires TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def cle(agent: str, officine: str, reference: str, nature: str) -> tuple:
        return (agent or "", officine or "", (reference or "").strip().upper(), nature or "commande")

    def reserver(self, cle: tuple, tache_id: str, destinataires: str = "") -> bool:
        """Inscrit l'envoi de cette tâche. False si un envoi vivant existe déjà pour la clé :
        alors rien ne doit partir."""
        with self._verrou:
            try:
                self._db.execute("INSERT INTO envois(agent, officine, reference, nature, tache, destinataires) VALUES (?, ?, ?, ?, ?, ?)",
                                 (*cle, tache_id, destinataires))
                return True
            except sqlite3.IntegrityError:
                return self._db.execute("SELECT 1 FROM envois WHERE tache = ?", (tache_id,)).fetchone() is not None

    def inscrire(self, cle: tuple, tache_id: str, vivant: bool, destinataires: str = "") -> None:
        """L'historique : un envoi d'avant le registre (ou vu au passage) s'y inscrit, sans
        contrainte, pour que « qui a porté cette référence » ne dépende jamais d'un parcours
        paginé du magasin (revue du 5 novembre)."""
        with self._verrou:
            self._db.execute("INSERT OR IGNORE INTO envois(agent, officine, reference, nature, tache, etat, destinataires) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?)", (*cle, tache_id, "vivant" if vivant else "ferme", destinataires))

    def porteurs(self, reference: str) -> set:
        """Qui a porté cette référence : les couples (agent, officine, destinataires), tous envois confondus."""
        with self._verrou:
            return {tuple(r) for r in self._db.execute(
                "SELECT agent, officine, destinataires FROM envois WHERE reference = ?", ((reference or "").strip().upper(),))}

    def fermer(self, tache_id: str) -> None:
        with self._verrou:
            self._db.execute("UPDATE envois SET etat = 'ferme' WHERE tache = ?", (tache_id,))

    def taches(self, cle: tuple) -> list[str]:
        with self._verrou:
            return [r[0] for r in self._db.execute(
                "SELECT tache FROM envois WHERE agent = ? AND officine = ? AND reference = ? AND nature = ?", cle)]

    # compatibilité : inscrire sans contrainte (sous le jeton unique, ou pour retrouver une tâche)
    def ajouter(self, cle: tuple, tache_id: str) -> None:
        self.reserver(cle, tache_id)
