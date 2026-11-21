import argparse
from datetime import datetime, time, timedelta, timezone
from http.server import ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from openpyxl import Workbook, load_workbook

from app import Handler, Service
from domain import Problem, ZONE, suggestions
from excel import parse_pod
from store import Store


REAL_POD = None


# Construye un POD de prueba en memoria para verificar el flujo sin archivos operativos.
def sample(kind='NOCHE', include_sheet=True, include_front=True):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'PROG' if include_sheet else 'Otra'
    sheet['D7'] = 'PROGRAMA PARA TURNO:'
    sheet['E7'] = kind
    sheet['D9'] = 'FRENTE'
    sheet['F9'] = 'PROCESO'
    for index in range(24):
        minute = ((1200 if kind == 'NOCHE' else 480) + index * 30) % 1440
        sheet.cell(9, index + 7, time(minute // 60, minute % 60))
    start = ['NP', 'N61', 'CAB-01 NP', 'Estado inicial', 'Programado']
    if include_front:
        for column, value in enumerate(start, 2):
            sheet.cell(10, column, value)
        for column in range(13, 17):
            sheet.cell(10, column, 'EXT')
        sheet['AE10'] = 'Cierre esperado'
        sheet['AF10'] = 1
        sheet['AH10'] = 1
        sheet['F11'] = 'Equipo Prog.'
        sheet['M11'] = 'D-01'
        sheet['N11'] = 'D-02'
        sheet['Q11'] = 'D-03'
        sheet['F12'] = 'Cuadrilla Prog.'
        sheet['M12'] = 'K1'
    for row_index, label in ((13, 'Frente sin programa'), (16, 'Frente sin actividades')):
        fields = ['EXT', 'N52', label, 'Estado inicial', 'Programado']
        for column, value in enumerate(fields, 2):
            sheet.cell(row_index, column, value)
        sheet.cell(row_index, 32, 1 if row_index == 13 else 0)
    sheet['G13'] = 'SP'
    with BytesIO() as output:
        workbook.save(output)
        return output.getvalue()


class AppTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.service = Service(self.path)

    def import_turn(self, day='2025-11-20', kind='Noche', replace=False, contents=None):
        contents = sample() if contents is None else contents
        preview = self.service.preview(contents, 'POD_20_11-2025.xlsm')
        return self.service.commit_import(dict(token=preview['token'], day=day, kind=kind, replace=replace))['turn']

    def real(self, turn, **changes):
        payload = dict(turn=turn['id'], front=turn['fronts'][0]['id'], action='real',
                       start=1395, end=1500, code='EXT', equipment=['D-REAL'], crew=['K-REAL'])
        return self.service.mutate_front({**payload, **changes})['turn']

    def test_night_template_and_sp(self):
        pod = REAL_POD.read_bytes() if REAL_POD else sample()
        preview = parse_pod(pod, 'POD_20_11-2025.xlsm')
        front = next(front for front in preview['fronts'] if front['name'] == 'CAB-01 NP')
        self.assertIn(dict(code='EXT', start=1380, end=1500), front['plan'])
        self.assertEqual((preview['start'], preview['end']), (1200, 1920))
        self.assertTrue(any(not front['plan'] for front in preview['fronts']))
        self.assertTrue(any(front['assignments'] for front in preview['fronts']))

    def test_synthetic_day_and_missing_sheet(self):
        day = parse_pod(sample('DIA'), 'POD.xlsx')
        self.assertEqual((day['kind'], day['start'], day['end']), ('Día', 480, 1200))
        with self.assertRaisesRegex(Problem, 'no contiene la hoja PROG'):
            parse_pod(sample(include_sheet=False), 'POD.xlsx')

    def test_realtime_suggestions_and_simultaneous_activities(self):
        turn = self.import_turn()
        turn = self.real(turn)
        front = turn['fronts'][0]
        now = datetime(2025, 11, 21, 1, 0, tzinfo=ZONE)
        self.assertEqual([slot['start'] for slot in suggestions(turn, now)[front['id']]], [1380])
        before = datetime(2025, 11, 20, 23, 1, tzinfo=ZONE)
        self.assertFalse(suggestions(turn, before)[front['id']])
        with self.assertRaisesRegex(Problem, 'dentro del turno'):
            self.real(turn, start=1190)
        with self.assertRaisesRegex(Problem, '15 minutos'):
            self.real(turn, start=1396)
        with self.assertRaisesRegex(Problem, 'se solapa'):
            self.real(turn, code='TRAS', end=1410)
        turn = self.real(turn, code='TRAS', end=1410, equipment=['D-OTRO'], simultaneous=True)
        self.assertEqual({row['code']: row['equipment'] for row in turn['fronts'][0]['real']},
                         {'EXT': ['D-REAL'], 'TRAS': ['D-OTRO']})

    def test_saved_data_survives_reimport_and_restart(self):
        turn = self.real(self.import_turn())
        front = turn['fronts'][0]
        base = {'turn': turn['id'], 'front': front['id']}
        self.service.mutate_front({**base, 'action': 'closing', 'note': 'Cierre real'})
        entry = self.service.save_catalog(dict(code='P001', primary='Prueba', secondary='Causa'))['catalog'][0]
        self.service.mutate_front({**base, 'action': 'cnc', 'start': 1380, 'end': 1395,
                                   'catalog': entry['id'], 'note': 'Registro de prueba'})
        self.service.save_catalog({**entry, 'secondary': 'Causa corregida'})
        preview = self.service.preview(sample(include_front=False), 'POD.xlsm')
        with self.assertRaisesRegex(Problem, 'frentes omitidos'):
            self.service.commit_import(dict(day=turn['day'], kind=turn['kind'], token=preview['token']))
        self.import_turn(replace=True, contents=sample(include_front=False))
        self.service = Service(self.path)
        with self.service.store.connection() as db:
            fresh = self.service.get_turn(db, turn['id'])
        retained = next(item for item in fresh['fronts'] if item['id'] == front['id'])
        self.assertTrue(retained['absent'])
        self.assertEqual(retained['closing'], 'Cierre real')
        self.assertEqual(retained['real'], front['real'])
        self.assertEqual(retained['cnc'][0]['secondary'], 'Causa')

    def test_plan_edit_move_and_copy_preserve_assignments(self):
        turn = self.import_turn()
        front = turn['fronts'][0]
        activity = front['plan'][0]
        base = {'turn': turn['id'], 'front': front['id'], 'id': activity['id']}
        result = self.service.mutate_front({**base, 'action': 'plan', 'start': 1380,
                                            'end': 1500, 'code': 'ac'})['turn']['fronts'][0]
        self.assertEqual(result['plan'][0]['code'], 'AC')
        self.assertCountEqual(result['assignments'], front['assignments'])

        place = {**base, 'action': 'place_activity', 'source_front': front['id'], 'source_field': 'plan'}
        result = self.service.mutate_front({**place, 'destination_field': 'plan', 'start': 1320})['turn']['fronts'][0]
        expected = [{'code': code, 'start': start, 'end': end, 'type': kind} for code, start, end, kind in (
            ('D-01', 1320, 1350, 'Equipo'), ('D-02', 1350, 1380, 'Equipo'),
            ('K1', 1320, 1350, 'Cuadrilla'), ('D-03', 1500, 1530, 'Equipo'))]
        self.assertCountEqual(result['assignments'], expected)

        copy = {**place, 'destination_field': 'real', 'start': 1410}
        result = self.service.mutate_front(copy)['turn']['fronts'][0]
        self.assertEqual(result['plan'][0], {**activity, 'start': 1320, 'end': 1440, 'code': 'AC'})
        real = result['real'][0]
        self.assertNotEqual(real['id'], activity['id'])
        self.assertEqual((real['code'], real['start'], real['end'], real['equipment'], real['crew']),
                         ('AC', 1410, 1530, ['D-01', 'D-02'], ['K1']))
        with self.assertRaisesRegex(Problem, 'se solapa'):
            self.service.mutate_front(copy)
        with self.assertRaisesRegex(Problem, 'dentro del turno'):
            self.service.mutate_front({**copy, 'start': 1905})
        with Service(self.path).store.connection() as db:
            persisted = self.service.get_turn(db, turn['id'])['fronts'][0]
        self.assertEqual(persisted, result)

    def test_resizing_and_partial_edits_keep_other_activity_data(self):
        turn = self.import_turn()
        front = turn['fronts'][0]
        base = {'turn': turn['id'], 'front': front['id']}
        plan = front['plan'][0]
        result = self.service.mutate_front({**base, 'action': 'plan', 'id': plan['id'],
                                            'start': 1365, 'end': 1500})['turn']['fronts'][0]
        self.assertEqual(result['plan'][0], {**plan, 'start': 1365})
        self.assertCountEqual([(row['code'], row['start'], row['end']) for row in result['assignments']],
                              [('D-01', 1365, 1410), ('D-02', 1410, 1440),
                               ('K1', 1365, 1410), ('D-03', 1500, 1530)])
        with self.assertRaisesRegex(Problem, 'cambió en otra vista'):
            self.service.mutate_front({**base, 'action': 'plan', 'id': plan['id'], 'start': 1380,
                                       'end': 1515, 'original_range': [1380, 1500]})
        updated = self.service.mutate_front({**base, 'action': 'plan', 'id': plan['id'],
                                             'crew': ['K2']})['turn']['fronts'][0]
        self.assertEqual(updated['plan'], result['plan'])
        self.assertEqual([row for row in updated['assignments'] if row['type'] == 'Cuadrilla'],
                         [dict(type='Cuadrilla', code='K2', start=1365, end=1500)])
        self.assertCountEqual([row for row in updated['assignments'] if row['type'] == 'Equipo'],
                              [row for row in result['assignments'] if row['type'] == 'Equipo'])

        real = self.real(turn)['fronts'][0]['real'][0]
        result = self.service.mutate_front({**base, 'action': 'real', 'id': real['id'],
                                            'start': 1395, 'end': 1515})['turn']['fronts'][0]
        self.assertEqual(result['real'], [{**real, 'end': 1515}])
        result = self.service.mutate_front({**base, 'action': 'real', 'id': real['id'],
                                            'equipment': []})['turn']['fronts'][0]
        self.assertEqual(result['real'], [{**real, 'end': 1515, 'equipment': []}])

    def test_periodic_backup_restores_turn_and_rotates_only_backup_files(self):
        turn = self.real(self.import_turn())
        store = self.service.store
        old = store.create_backup(datetime.now(timezone.utc) - timedelta(days=8))
        retained = store.create_backup(datetime.now(timezone.utc) - timedelta(days=2))
        self.assertTrue(old.exists())
        manual = retained.with_name('copia_manual.sqlite3')
        manual.touch()
        stop = Event()
        completed = Event()
        create_backup = store.create_backup

        def create_and_signal():
            path = create_backup()
            completed.set()
            stop.set()
            return path

        with patch.object(store, 'create_backup', side_effect=create_and_signal):
            worker = Thread(target=self.service.run_backups, args=(stop, 0.01), daemon=True)
            worker.start()
            try:
                self.assertTrue(completed.wait(5), 'No se creó el respaldo automático')
            finally:
                stop.set()
                worker.join(5)
        self.assertFalse(old.exists())
        self.assertTrue(retained.exists())
        self.assertTrue(manual.exists())
        snapshots = list(retained.parent.glob('turnos_*_UTC.sqlite3'))
        latest = next(path for path in snapshots if path != retained)
        with Store(latest).connection() as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(store.get_turn(db, turn['id']), turn)

    def test_excel_create_update_preserve_and_recover(self):
        first = self.real(self.import_turn())
        second = self.import_turn(day='2025-11-21')
        destination = self.path / 'Seguimiento.xlsx'
        first_args = dict(turn=first['id'], path=str(destination))
        self.service.export(first_args)
        book = load_workbook(destination)
        book.create_sheet('Indicadores')['A1'] = '=SUM(1,2)'
        book.save(destination)
        book.close()
        self.service.export(dict(turn=second['id'], path=str(destination)))
        activity = first['fronts'][0]['real'][0]
        self.real(first, id=activity['id'], end=1485)
        self.service.export(first_args)
        self.service.export(first_args)
        book = load_workbook(destination)
        try:
            self.assertEqual(book['Indicadores']['A1'].value, '=SUM(1,2)')
            rows = list(book['Actividades Plan y Real'].values)[1:]
            self.assertEqual({row[0] for row in rows}, {first['id'], second['id']})
            real = [row for row in rows if row[5] == 'Real']
            self.assertEqual(len(real), 1)
            self.assertEqual(real[0][8], datetime(2025, 11, 21, 0, 45))
            resources = list(book['Asignaciones'].values)[1:]
            self.assertTrue(any(row[4] is None and row[7] == 'D-03' for row in resources))
            self.assertTrue(all(book[name].tables for name in
                ('Frentes por turno', 'Actividades Plan y Real', 'Asignaciones', 'CNC')))
        finally:
            book.close()
        original = destination.read_bytes()
        with patch('excel.os.replace', side_effect=PermissionError('Simula Excel abierto')):
            with self.assertRaisesRegex(Problem, 'registro local se conservó'):
                self.service.export(first_args)
        self.assertEqual(destination.read_bytes(), original)
        self.service.export(first_args)

    def test_http_endpoints(self):
        with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
            server.service = self.service
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                with urlopen(base, timeout=3) as response:
                    page = response.read()
                    self.assertIn(b'Seguimiento de Turnos', page)
                    self.assertIn(b'href="/historial" target="_blank"', page)
                with urlopen(base + '/historial', timeout=3) as response:
                    self.assertIn(b'Solo lectura', response.read())
                preview_request = Request(base + '/api/preview', sample(), {'X-File-Name': 'POD.xlsm'})
                with urlopen(preview_request, timeout=3) as response:
                    preview = json.load(response)
                payload = {'token': preview['token'], 'day': '2025-11-20', 'kind': 'Noche'}
                confirm = Request(base + '/api/import', json.dumps(payload).encode(), {'Content-Type': 'application/json'})
                with urlopen(confirm, timeout=3) as response:
                    turn = json.load(response)['turn']
                with urlopen(base + '/api/turn?id=' + quote(turn['id']), timeout=3) as response:
                    self.assertEqual(json.load(response)['turn']['id'], turn['id'])
                with self.assertRaises(HTTPError) as caught:
                    urlopen(Request(base + '/api/data', headers={'Origin': 'http://otro.test'}), timeout=3)
                self.assertEqual(caught.exception.code, 403)
            finally:
                server.shutdown()
                worker.join(timeout=3)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(add_help=False)
    # El POD real es opcional y se proporciona desde fuera del repositorio.
    parser.add_argument('--pod', type=Path)
    args, remainder = parser.parse_known_args()
    REAL_POD = args.pod
    unittest.main(argv=['test_app.py', *remainder])
