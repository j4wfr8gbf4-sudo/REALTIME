import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import sqlite3
from threading import Event, Lock, Thread
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import uuid4
import webbrowser

from domain import (
    Problem, assemble_turn, clean, codes, now_minute, overlap,
    range_of, required, slot_state, suggestions, turn_id,
)
from excel import parse_pod, write_export
from store import Store


MAX_UPLOAD = 20 * 1024 * 1024
BACKUP_INTERVAL = 15 * 60
STATIC = Path(__file__).parent / 'static'
ASSETS = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/historial': ('index.html', 'text/html; charset=utf-8'),
    '/app.css': ('app.css', 'text/css; charset=utf-8'),
    '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
}


# El servicio coordina las reglas del turno, la base SQLite y los libros Excel.
class Service:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.store = Store(data_dir / 'turnos.sqlite3')
        self.previews = {}
        self.preview_lock = Lock()
        self.export_lock = Lock()

    def get_turn(self, db, key):
        turn = self.store.get_turn(db, key)
        if turn is None:
            raise Problem('El turno seleccionado no existe.', 404)
        return turn

    def run_backups(self, stop, interval=BACKUP_INTERVAL):
        while not stop.wait(interval):
            try:
                self.store.create_backup()
            except (OSError, sqlite3.Error):
                logging.exception('Error al respaldar la base de turnos. Se reintentará en el siguiente intervalo.')

    def get_data(self):
        with self.store.connection() as db:
            return {
                'turns': self.store.list_turns(db), 'catalog': self.store.catalog(db),
                'export_path': self.store.export_path(db) or str(self.data_dir / 'Seguimiento_Turnos.xlsx'),
            }

    def view(self, turn):
        return {'turn': turn, 'suggestions': suggestions(turn)}

    def preview(self, contents, filename):
        parsed = parse_pod(contents, filename)
        # La vista previa vive en memoria hasta que el usuario confirma la fecha y el turno.
        token = uuid4().hex
        with self.preview_lock:
            if len(self.previews) >= 8:
                self.previews.pop(next(iter(self.previews)))
            self.previews[token] = parsed
        return {'token': token, **parsed}

    def commit_import(self, payload):
        day, kind, token = payload.get('day'), payload.get('kind'), clean(payload.get('token'))
        key = turn_id(day, kind)
        with self.preview_lock:
            preview = self.previews.get(token)
        if preview is None:
            raise Problem('La vista previa caducó. Vuelve a cargar el POD.')
        with self.store.connection(write=True) as db:
            previous = self.store.get_turn(db, key)
            turn = assemble_turn(preview, day, kind, previous)
            # La interfaz interpreta el 409 como una solicitud de confirmación del reemplazo.
            if previous is not None and payload.get('replace') is not True:
                retained = [front['name'] for front in turn['fronts'] if front['absent']]
                suffix = f' Se conservarán los registros de los frentes omitidos: {", ".join(retained)}.' if retained else ''
                raise Problem(f'Ya existe este turno. Confirma el reemplazo del Plan.{suffix}', 409)
            self.store.save_turn(db, turn)
        with self.preview_lock:
            self.previews.pop(token, None)
        return self.view(turn)

    # La lectura, las validaciones y el guardado comparten una transacción de escritura.
    def mutate_front(self, payload):
        with self.store.connection(write=True) as db:
            turn = self.get_turn(db, payload.get('turn'))
            front = next((item for item in turn['fronts'] if item['id'] == payload.get('front')), None)
            if front is None:
                raise Problem('El frente no existe en este turno.', 404)
            action = payload.get('action')
            if action == 'closing':
                front['closing'] = clean(payload.get('note'))
            elif action in ('plan', 'real'):
                # El ajuste de duración compara el rango que el usuario vio con el actual.
                if 'original_range' in payload:
                    entry = self.activity(front, action, payload.get('id'))
                    if [entry['start'], entry['end']] != payload['original_range']:
                        raise Problem('El horario cambió en otra vista. Recarga el turno antes de volver a ajustarlo.', 409)
                save = self.save_plan if action == 'plan' else self.save_real
                save(turn, front, payload)
            elif action == 'place_activity':
                self.place_activity(turn, front, payload)
            elif action == 'cnc':
                self.save_cnc(db, turn, front, payload)
            elif action == 'dismiss':
                start, end = range_of(turn, payload)
                elapsed = now_minute(turn['day'])
                for minute in range(start, end, 15):
                    if state := slot_state(front, minute, elapsed):
                        front['dismissed'][str(minute)] = state['signature']
            elif action == 'remove_plan':
                entry = self.activity(front, 'plan', payload.get('id'))
                self.take_plan_assignments(front, entry)
                front['plan'].remove(entry)
            elif action in ('remove_real', 'remove_cnc'):
                field = action.removeprefix('remove_')
                front[field] = [row for row in front[field] if row['id'] != payload.get('id')]
            else:
                raise Problem('Acción desconocida.')
            self.store.save_turn(db, turn)
        return self.view(turn)

    @staticmethod
    def activity(front, field, identifier):
        entry = next((row for row in front[field] if row['id'] == identifier), None)
        if entry is None:
            raise Problem('La actividad ya no está disponible.', 404)
        return entry

    # Separa el tramo asociado a la actividad y conserva los extremos que quedan fuera.
    @staticmethod
    def take_plan_assignments(front, entry):
        start, end = entry['start'], entry['end']
        attached, remaining = [], []
        for row in front['assignments']:
            if not overlap(row, start, end):
                remaining.append(row)
                continue
            attached.append({**row, 'start': max(row['start'], start), 'end': min(row['end'], end)})
            if row['start'] < start:
                remaining.append({**row, 'end': start})
            if end < row['end']:
                remaining.append({**row, 'start': end})
        front['assignments'] = remaining
        return attached

    def save_plan(self, turn, front, payload, source=None):
        identifier = clean(payload.get('id'))
        original_front = source if source is not None else front
        original = self.activity(original_front, 'plan', identifier) if identifier else None
        start, end = range_of(turn, {**(original or {}), **payload})
        if any(row['id'] != identifier and overlap(row, start, end) for row in front['plan']):
            raise Problem('El rango está ocupado por otra actividad del Plan.', 409)
        code = required(payload.get('code', original['code'] if original else None), 'el código de actividad').upper()
        if code == 'SP':
            raise Problem('SP identifica frentes sin programa. Ingresa una sigla de actividad.')

        attached = self.take_plan_assignments(original_front, original) if original else []
        for kind, key in (('Equipo', 'equipment'), ('Cuadrilla', 'crew')):
            previous = [row for row in attached if row['type'] == kind]
            selected = codes(payload[key]) if key in payload else None
            if selected is not None and set(selected) != {row['code'] for row in previous}:
                front['assignments'].extend({'type': kind, 'code': value, 'start': start, 'end': end}
                                            for value in selected)
            # Al mover se desplazan las asignaciones. Al redimensionar se ajustan al rango nuevo.
            elif original:
                resizing = original_front is front and (start == original['start'] or end == original['end'])
                shift = 0 if resizing else start - original['start']
                for row in previous:
                    new_start = start if row['start'] == original['start'] else max(start, row['start'] + shift)
                    new_end = end if row['end'] == original['end'] else min(end, row['end'] + shift)
                    if new_start < new_end:
                        front['assignments'].append({**row, 'start': new_start, 'end': new_end})
        if original:
            original_front['plan'].remove(original)
        front['plan'].append({'id': identifier or uuid4().hex, 'start': start, 'end': end, 'code': code})
        front['plan'].sort(key=lambda row: (row['start'], row['end']))

    def place_activity(self, turn, front, payload):
        source_field, destination_field = payload.get('source_field'), payload.get('destination_field')
        if (source_field, destination_field) not in (('plan', 'real'), ('plan', 'plan'), ('real', 'real')):
            raise Problem('Usa una fila del mismo tipo o copia de Plan a Real.')
        source = next((row for row in turn['fronts'] if row['id'] == payload.get('source_front')), None)
        if source is None:
            raise Problem('El frente de origen ya no está disponible.', 404)
        entry = self.activity(source, source_field, payload.get('id'))
        start = payload.get('start')
        if type(start) is not int:
            raise Problem('Selecciona una hora de inicio.')
        end = start + entry['end'] - entry['start']
        values = {'start': start, 'end': end, 'code': entry['code']}
        if destination_field == 'plan':
            self.save_plan(turn, front, {**values, 'id': entry['id']}, source)
            return
        # Al copiar a Real, los recursos se toman de las asignaciones que coinciden con el Plan.
        if source_field == 'plan':
            for kind, key in (('Equipo', 'equipment'), ('Cuadrilla', 'crew')):
                values[key] = [row['code'] for row in source['assignments'] if row['type'] == kind
                               and overlap(row, entry['start'], entry['end'])]
            self.save_real(turn, front, values)
            return
        values.update(equipment=entry['equipment'], crew=entry['crew'])
        if source is front:
            self.save_real(turn, front, {**values, 'id': entry['id']})
        else:
            moved = self.save_real(turn, front, values)
            moved['id'] = entry['id']
            source['real'].remove(entry)

    @staticmethod
    def save_real(turn, front, payload):
        identifier = clean(payload.get('id'))
        existing = next((row for row in front['real'] if row['id'] == identifier), None)
        if identifier and existing is None:
            raise Problem('La actividad real ya no está disponible.', 404)
        start, end = range_of(turn, {**(existing or {}), **payload})
        for row in front['real']:
            if row['id'] == identifier or not overlap(row, start, end):
                continue
            if payload.get('simultaneous') is True:
                break
            # Una edición puede conservar o reducir un solapamiento ya registrado.
            if existing and max(start, row['start']) >= max(existing['start'], row['start']) and (
                min(end, row['end']) <= min(existing['end'], row['end'])
            ) and overlap(row, existing['start'], existing['end']):
                continue
            raise Problem('El rango se solapa con otra actividad. Edita ese registro o usa «Agregar simultánea».', 409)
        defaults = existing or {'code': None, 'equipment': [], 'crew': []}
        activity = {
            'id': identifier or uuid4().hex, 'start': start, 'end': end,
            'code': required(payload.get('code', defaults['code']), 'el código de actividad').upper(),
            'equipment': codes(payload.get('equipment', defaults['equipment'])),
            'crew': codes(payload.get('crew', defaults['crew'])),
        }
        if activity['code'] == 'SP':
            raise Problem('SP identifica frentes sin programa. Selecciona un código de actividad real.')
        if existing is not None:
            existing.update(activity)
        else:
            front['real'].append(activity)
        front['real'].sort(key=lambda item: (item['start'], item['end']))
        return existing if existing is not None else activity

    def save_cnc(self, db, turn, front, payload):
        start, end = range_of(turn, payload)
        identifier = clean(payload.get('id'))
        existing = next((row for row in front['cnc'] if row['id'] == identifier), None)
        if identifier and existing is None:
            raise Problem('La CNC ya no está disponible.', 404)
        catalog_id = payload.get('catalog')
        entry = next((row for row in self.store.catalog(db) if row['id'] == catalog_id), None)
        # Una CNC conserva las clasificaciones capturadas al registrarla con ese código de catálogo.
        if existing and (not catalog_id or catalog_id == existing.get('catalog')):
            entry = {field: existing[field] for field in ('code', 'primary', 'secondary')}
            catalog_id = existing.get('catalog')
        if entry is None:
            raise Problem('Selecciona una combinación de código y clasificaciones del catálogo.')
        if not existing and end > now_minute(turn['day']):
            raise Problem('La CNC debe corresponder a bloques cuyo término ya pasó.')
        if any(row['id'] != identifier and overlap(row, start, end) for row in front['cnc']):
            raise Problem('Ya hay una CNC confirmada que se solapa con el rango.', 409)
        values = {
            'id': identifier or uuid4().hex, 'start': start, 'end': end,
            'catalog': catalog_id, 'note': clean(payload.get('note')),
            **{field: entry[field] for field in ('code', 'primary', 'secondary')},
        }
        if existing is not None:
            existing.update(values)
        else:
            front['cnc'].append(values)

    def save_catalog(self, payload):
        identifier = clean(payload.get('id')) or uuid4().hex
        labels = (required(payload.get('code'), 'el código').upper(),
                  required(payload.get('primary'), 'la clasificación primaria'),
                  required(payload.get('secondary'), 'la clasificación secundaria'))
        with self.store.connection(write=True) as db:
            try:
                db.execute("""INSERT INTO catalog (id, code, primary_label, secondary_label) VALUES (?, ?, ?, ?)
                           ON CONFLICT(id) DO UPDATE SET code=excluded.code,
                           primary_label=excluded.primary_label, secondary_label=excluded.secondary_label""",
                           (identifier, *labels))
            except sqlite3.IntegrityError as exc:
                raise Problem('Ya existe esa combinación en el catálogo.', 409) from exc
            return {'catalog': self.store.catalog(db)}

    def export(self, payload):
        destination = required(payload.get('path'), 'la ruta del archivo de seguimiento')
        path = Path(destination).expanduser()
        if not path.is_absolute():
            path = self.data_dir / path
        path = path.resolve()
        with self.export_lock:
            with self.store.connection() as db:
                turn = self.get_turn(db, payload.get('turn'))
            write_export(path, turn)
            with self.store.connection(write=True) as db:
                self.store.save_export_path(db, path)
        return {'path': str(path)}


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, content_type, content):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(content)

    def json_reply(self, status, data):
        self.reply(status, 'application/json; charset=utf-8', json.dumps(data, ensure_ascii=False).encode())

    def get_body(self, size_limit):
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError as exc:
            raise Problem('Solicitud inválida.') from exc
        if not 0 < length <= size_limit:
            raise Problem('El archivo supera 20 MB o la solicitud está vacía.', 413)
        return self.rfile.read(length)

    def get_payload(self):
        if self.headers.get_content_type() != 'application/json':
            raise Problem('Formato de solicitud inválido.')
        try:
            payload = json.loads(self.get_body(128 * 1024))
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise Problem('Solicitud inválida.') from exc
        if not isinstance(payload, dict):
            raise Problem('Solicitud inválida.')
        return payload

    def dispatch(self, method):
        try:
            port = self.server.server_port
            # Host y Origin deben corresponder al servidor que escucha en el equipo local.
            hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
            origin = self.headers.get('Origin')
            if self.headers.get('Host') not in hosts or (origin and origin not in {f'http://{host}' for host in hosts}):
                raise Problem('La aplicación solo admite solicitudes desde el navegador local.', 403)
            url = urlsplit(self.path)
            service = self.server.service
            if method == 'GET':
                if url.path in ASSETS:
                    filename, content_type = ASSETS[url.path]
                    self.reply(200, content_type, (STATIC / filename).read_bytes())
                    return
                if url.path == '/api/data':
                    result = service.get_data()
                elif url.path == '/api/turn':
                    key = parse_qs(url.query).get('id', [''])[0]
                    with service.store.connection() as db:
                        result = service.view(service.get_turn(db, key))
                else:
                    raise Problem('Ruta no encontrada.', 404)
            # La vista previa recibe el archivo binario. Las demás operaciones reciben JSON.
            elif url.path == '/api/preview':
                filename = Path(unquote(self.headers.get('X-File-Name', ''))).name
                if not filename.lower().endswith(('.xlsx', '.xlsm')):
                    raise Problem('Selecciona un POD en formato .xlsx o .xlsm.')
                result = service.preview(self.get_body(MAX_UPLOAD), filename)
            else:
                operations = {
                    '/api/import': service.commit_import, '/api/front': service.mutate_front,
                    '/api/catalog': service.save_catalog, '/api/export': service.export,
                }
                operation = operations.get(url.path)
                if operation is None:
                    raise Problem('Ruta no encontrada.', 404)
                result = operation(self.get_payload())
            self.json_reply(200, result)
        except Problem as exc:
            self.json_reply(exc.status, {'error': str(exc)})
        except (OSError, sqlite3.Error, ValueError):
            logging.exception('Error al procesar la solicitud local')
            self.json_reply(500, {'error': 'No se pudo completar la operación. Revisa los archivos e inténtalo otra vez.'})

    def do_GET(self):
        self.dispatch('GET')

    def do_POST(self):
        self.dispatch('POST')


def main():
    parser = argparse.ArgumentParser(description='Tablero local de seguimiento de turnos.')
    parser.add_argument('--puerto', type=int, default=8765)
    parser.add_argument('--datos', type=Path, default=Path(__file__).parent / 'datos')
    parser.add_argument('--sin-navegador', action='store_true')
    args = parser.parse_args()
    data_dir = args.datos.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    with ThreadingHTTPServer(('127.0.0.1', args.puerto), Handler) as server:
        server.service = Service(data_dir)
        url = f'http://127.0.0.1:{server.server_port}'
        print(f'Abre {url}. Para detener la aplicación, presiona Ctrl+C.', flush=True)
        if not args.sin_navegador:
            webbrowser.open(url)
        # El ciclo de respaldos se ejecuta en paralelo y se detiene al cerrar el servidor.
        stop_backups = Event()
        backups = Thread(target=server.service.run_backups, args=(stop_backups,), daemon=True)
        backups.start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop_backups.set()
            backups.join(timeout=12)


if __name__ == '__main__':
    main()
