# Persistencia en SQLite. Cada operación usa su propia conexión y puede
# agrupar una lectura y sus escrituras en la misma transacción.
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
from tempfile import mkstemp


class Store:
    def __init__(self, path):
        self.path = path
        # Un turno se guarda como un documento JSON. El catálogo y las opciones tienen tablas propias.
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS turns (
                    id TEXT PRIMARY KEY, day TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS catalog (
                    id TEXT PRIMARY KEY, code TEXT NOT NULL,
                    primary_label TEXT NOT NULL, secondary_label TEXT NOT NULL,
                    UNIQUE (code, primary_label, secondary_label)
                );
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            # Reserva la escritura antes de leer un turno que se va a modificar.
            if write:
                db.execute('BEGIN IMMEDIATE')
            # El bloque llamador usa la conexión. Sus cambios se confirman si termina bien.
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def create_backup(self, now=None):
        now = now or datetime.now(timezone.utc)
        folder = self.path.parent / 'respaldos'
        folder.mkdir(exist_ok=True)
        destination = folder / now.strftime('turnos_%Y%m%d_%H%M%S_%f_UTC.sqlite3')
        # SQLite genera una copia consistente y el archivo final se publica al completarla.
        fd, temporary_name = mkstemp(prefix='.turnos_', suffix='.tmp', dir=folder)
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            with self.connection() as source, closing(sqlite3.connect(temporary)) as target:
                source.backup(target)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

        # La rotación usa las fechas UTC de los nombres generados por la aplicación.
        cutoff = now - timedelta(days=7)
        for backup in folder.glob('turnos_*_UTC.sqlite3'):
            try:
                created = datetime.strptime(backup.name, 'turnos_%Y%m%d_%H%M%S_%f_UTC.sqlite3')
            except ValueError:
                continue
            if created.replace(tzinfo=timezone.utc) < cutoff:
                backup.unlink(missing_ok=True)
        return destination

    @staticmethod
    def get_turn(db, key):
        row = db.execute('SELECT data FROM turns WHERE id = ?', (key,)).fetchone()
        return json.loads(row['data']) if row else None

    # Actualiza el documento del turno si la clave ya existe, o crea uno nuevo.
    @staticmethod
    def save_turn(db, turn):
        db.execute("""INSERT INTO turns (id, day, kind, data) VALUES (?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET data=excluded.data""",
                   (turn['id'], turn['day'], turn['kind'], json.dumps(turn, ensure_ascii=False)))

    # El selector del historial solo necesita el resumen de cada turno.
    @staticmethod
    def list_turns(db):
        return [dict(row) for row in db.execute('SELECT id, day, kind FROM turns ORDER BY day DESC, kind DESC')]

    @staticmethod
    def catalog(db):
        return [dict(row) for row in db.execute(
            'SELECT id, code, primary_label AS "primary", secondary_label AS "secondary" '
            'FROM catalog ORDER BY code, primary_label, secondary_label')]

    @staticmethod
    def export_path(db):
        row = db.execute("SELECT value FROM settings WHERE key='export_path'").fetchone()
        return row['value'] if row else None

    @staticmethod
    def save_export_path(db, path):
        db.execute("""INSERT INTO settings (key, value) VALUES ('export_path', ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (str(path),))
