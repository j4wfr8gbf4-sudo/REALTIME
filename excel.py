# Entrada y salida de Excel. La importación lee el Plan de PROG. La
# exportación reúne los registros de cada turno en cuatro tablas relacionadas.
from datetime import datetime, time
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile
import os
import re
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from openpyxl import Workbook, load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.worksheet.table import Table, TableStyleInfo

from domain import LEVELS, Problem, clean, wall_datetime


PLAN_TYPES = {'programado': 'plan', 'equipo prog.': 'Equipo', 'cuadrilla prog.': 'Cuadrilla'}
DAY_MATCH = re.compile(r'(?<!\d)(\d{1,2})[_.-](\d{1,2})[_.-](\d{4})(?!\d)')
MAX_ROWS = 6000


def header_minute(value):
    if isinstance(value, (time, datetime)):
        return value.hour * 60 + value.minute if not (value.second or value.microsecond) else None
    match = re.fullmatch(r'(\d{1,2}):(\d{2})(?::00)?', clean(value))
    if match:
        hour, minute = map(int, match.groups())
        if hour < 24 and minute < 60:
            return hour * 60 + minute
    return None


def program_hours(rows, header, kind):
    # Los 24 encabezados deben cubrir el turno de 12 horas. El reloj visual vuelve a cero a medianoche.
    expected = 480 if kind == 'Día' else 1200
    candidates = []
    for row in (rows[header], rows[header - 1] if header else []):
        minutes = [header_minute(value) for value in row[6:30]]
        counts = [minute == (expected + offset * 30) % 1440 for offset, minute in enumerate(minutes)]
        if len(counts) == 24 and all(counts):
            return expected, expected + 720
        candidates.append(minutes)
    if not any(candidates):
        raise Problem('No se encontraron horarios en PROG.')
    raise Problem(f'Los encabezados de PROG no forman 24 bloques de 30 minutos válidos para {kind}. Guarda el POD con sus fórmulas calculadas y vuelve a cargarlo.')


def grouped(values, start):
    result = []
    for offset, value in enumerate(values):
        if not value:
            continue
        minute = start + offset * 30
        # Une celdas contiguas con la misma sigla y mantiene los huecos del horario.
        if result and result[-1]['code'] == value and result[-1]['end'] == minute:
            result[-1]['end'] += 30
        else:
            result.append({'code': value, 'start': minute, 'end': minute + 30})
    return result


def suggest_day(filename):
    match = DAY_MATCH.search(filename)
    if match:
        day, month, year = map(int, match.groups())
        try:
            return datetime(year, month, day).date().isoformat()
        except ValueError:
            return ''
    return ''


def parse_pod(contents, filename):
    try:
        # Lee los valores guardados por Excel, incluidos los resultados de fórmulas ya calculadas.
        workbook = load_workbook(BytesIO(contents), read_only=True, data_only=True)
    except (BadZipFile, InvalidFileException, OSError, ValueError, KeyError, SyntaxError, ParseError) as exc:
        raise Problem('No se pudo abrir el Excel. Selecciona un archivo POD válido.') from exc
    try:
        if 'PROG' not in workbook.sheetnames:
            raise Problem('El archivo no contiene la hoja PROG.')
        sheet = workbook['PROG']
        if sheet.max_row and sheet.max_row > MAX_ROWS:
            raise Problem(f'La hoja PROG supera las {MAX_ROWS} filas admitidas.')
        rows = list(sheet.iter_rows(max_row=min(sheet.max_row or MAX_ROWS, MAX_ROWS), max_col=34, values_only=True))
    finally:
        workbook.close()
    # La posición del encabezado se detecta por las etiquetas de la plantilla.
    header = next((index for index, row in enumerate(rows[:40])
                   if clean(row[3]).upper() == 'FRENTE' and clean(row[5]).upper() == 'PROCESO'), None)
    if header is None:
        raise Problem('No se encontró el encabezado FRENTE / PROCESO en PROG.')
    kind = next((kind for row in rows[:header]
                 if 'PROGRAMA PARA TURNO' in clean(row[3]).upper()
                 if (kind := {'dia': 'Día', 'día': 'Día', 'noche': 'Noche'}.get(clean(row[4]).casefold()))), None)
    if kind is None:
        raise Problem('No se encontró el indicador Día/Noche en PROG.')
    start, end = program_hours(rows, header, kind)
    fronts, warnings = [], []
    current = None
    identities = set()
    # Cada fila Programado abre un grupo. Las filas de recursos siguientes se asocian a ese frente.
    for i, row in enumerate(rows[header + 1:], header + 2):
        flavor = PLAN_TYPES.get(clean(row[5]).casefold())
        values = [clean(value) for value in row[6:30]]
        level, block, name = (clean(value) for value in row[1:4])
        if flavor == 'plan':
            current = None
            if not name:
                if level or block or any(values):
                    warnings.append(f'Fila {i}. Falta el nombre del frente y se omitió el grupo.')
                continue
            if not level or not block:
                raise Problem(f'Fila {i}. Falta el nivel o macrobloque del frente {name}. Guarda el POD con sus fórmulas calculadas.')
            if level not in LEVELS:
                warnings.append(f'Fila {i}. Nivel fuera del catálogo habitual: {level}.')
            identity = (level.casefold(), block.casefold(), name.casefold())
            if identity in identities:
                raise Problem(f'Fila {i}. El frente {name} aparece duplicado para {level} y {block}.')
            identities.add(identity)
            # SP marca un frente sin programa y queda fuera de los bloques de actividades.
            has_sp = any(value.upper() == 'SP' for value in values)
            plan = grouped([value.upper() if value.upper() != 'SP' else '' for value in values], start)
            if has_sp and plan:
                warnings.append(f'Fila {i}. {name} combina SP y actividades. Se conservan las actividades.')
            if row[31] is None:
                warnings.append(f'Fila {i}. ACT/PROG no tiene un valor guardado. Se revisó el horario para identificar si hay Plan.')
            current = {
                'level': level, 'block': block, 'name': name,
                'opening': clean(row[4]), 'expected': clean(row[30]),
                'source_id': clean(row[33]), 'source_active': clean(row[31]),
                'plan': plan, 'assignments': [],
            }
            fronts.append(current)
        elif flavor and any(values):
            if current is None:
                warnings.append(f'Fila {i}. Hay recursos sin un frente identificable.')
                continue
            if name and (level.casefold(), block.casefold(), name.casefold()) != (
                    current['level'].casefold(), current['block'].casefold(), current['name'].casefold()):
                raise Problem(f'Fila {i}. Los recursos no corresponden al frente de la fila Programado.')
            current['assignments'].extend({**item, 'type': flavor} for item in grouped(values, start))
        elif not flavor:
            if any(values):
                warnings.append(f'Fila {i}. Tipo de fila no reconocido y omitido.')
            current = None
    # Sin frentes válidos, la vista previa se rechaza antes de guardar un turno.
    if not fronts:
        raise Problem('PROG no contiene frentes válidos para importar.')
    return {'kind': kind, 'day': suggest_day(filename), 'source': filename, 'start': start, 'end': end,
            'fronts': fronts, 'warnings': warnings}


# Las cuatro hojas comparten estas columnas para relacionar sus registros.
COMMON = ('ID turno', 'Fecha operativa', 'Turno', 'ID frente por turno')
SHEETS = {
    'Frentes por turno': ('FrentesPorTurno', COMMON + (
        'Macrobloque', 'Nivel', 'Frente', 'Estado inicial', 'Cierre esperado', 'Cierre real')),
    'Actividades Plan y Real': ('ActividadesPlanReal', COMMON + (
        'ID actividad', 'Tipo', 'Código', 'Inicio', 'Término', 'Duración (min)')),
    'Asignaciones': ('AsignacionesTurno', COMMON + (
        'ID actividad', 'Plan/Real', 'Tipo de recurso', 'Código', 'Inicio', 'Término')),
    'CNC': ('CNCTurno', COMMON + (
        'ID CNC', 'Inicio', 'Término', 'Duración (min)', 'Código',
        'Clasificación primaria', 'Clasificación secundaria', 'Observación')),
}
DATE_FORMAT = 'dd-mm-yyyy'
TIME_FORMAT = 'dd-mm-yyyy hh:mm'


# Divide el intervalo por actividad y conserva los tramos sin ID de actividad.
def assignment_spans(assignment, activities):
    start, end = assignment['start'], assignment['end']
    cursor = start
    for activity in activities:
        overlap_start = max(activity['start'], start)
        overlap_end = min(activity['end'], end)
        if overlap_start >= overlap_end:
            continue
        if cursor < overlap_start:
            yield None, cursor, overlap_start
        yield activity['id'], overlap_start, overlap_end
        cursor = overlap_end
    if cursor < end:
        yield None, cursor, end


def export_rows(turn):
    rows = {name: [] for name in SHEETS}
    day = turn['day']
    date_value = datetime.fromisoformat(day).date()
    for front in turn['fronts']:
        common = (turn['id'], date_value, turn['kind'], front['id'])
        rows['Frentes por turno'].append(common + (
            front['block'], front['level'], front['name'], front['opening'], front['expected'], front['closing']))
        for kind, field in (('Plan', 'plan'), ('Real', 'real')):
            for activity in front[field]:
                start, end = activity['start'], activity['end']
                rows['Actividades Plan y Real'].append(common + (
                    activity['id'], kind, activity['code'], wall_datetime(day, start),
                    wall_datetime(day, end), end - start))
                # Los recursos de Real cubren todo el intervalo de la actividad.
                if kind == 'Real':
                    for resource_type, key in (('Equipo', 'equipment'), ('Cuadrilla', 'crew')):
                        for code in activity[key]:
                            rows['Asignaciones'].append(common + (
                                activity['id'], kind, resource_type, code,
                                wall_datetime(day, start), wall_datetime(day, end)))
        # En Plan, una asignación puede cubrir parte de una actividad o pasar por varias.
        for assignment in front['assignments']:
            for activity_id, start, end in assignment_spans(assignment, front['plan']):
                rows['Asignaciones'].append(common + (
                    activity_id, 'Plan', assignment['type'], assignment['code'],
                    wall_datetime(day, start), wall_datetime(day, end)))
        for cnc in front['cnc']:
            start, end = cnc['start'], cnc['end']
            rows['CNC'].append(common + (cnc['id'], wall_datetime(day, start), wall_datetime(day, end),
                end - start, cnc['code'], cnc['primary'], cnc['secondary'], cnc['note']))
    return rows


# Reemplaza las filas del turno actual y ajusta los rangos de tabla y filtro.
def replace_sheet_rows(sheet, table_name, headers, turn_key, new_rows):
    if sheet.max_row > 1:
        for number in reversed([cells[0].row for cells in sheet.iter_rows(min_row=2, max_col=1)
                                if cells[0].value == turn_key]):
            sheet.delete_rows(number)
    if sheet['A1'].value is None:
        for column, header in enumerate(headers, 1):
            sheet.cell(1, column, header)
    for values in new_rows:
        sheet.append(values)
        for cell in sheet[sheet.max_row]:
            if isinstance(cell.value, datetime):
                cell.number_format = TIME_FORMAT
            elif cell.column == 2:
                cell.number_format = DATE_FORMAT
            # Los valores escritos por el usuario se exportan como texto aunque parezcan fórmulas o errores.
            elif isinstance(cell.value, str) and cell.data_type in ('f', 'e'):
                cell.data_type = 's'
    ref = f'A1:{get_column_letter(len(headers))}{max(2, sheet.max_row)}'
    if table_name in sheet.tables:
        table = sheet.tables[table_name]
        table.ref = ref
        if table.autoFilter is not None:
            table.autoFilter.ref = ref
    else:
        table = Table(displayName=table_name, ref=ref)
        table.tableStyleInfo = TableStyleInfo(name='TableStyleMedium2', showRowStripes=True)
        sheet.add_table(table)
        sheet.freeze_panes = 'E2'
        for index, header in enumerate(headers, 1):
            sheet.column_dimensions[get_column_letter(index)].width = max(18, min(34, len(header) + 5))


# Comprueba que las hojas, encabezados y tablas correspondan a la base de la aplicación.
def verify_schema(workbook):
    for name, (table_name, headers) in SHEETS.items():
        if name not in workbook.sheetnames:
            raise Problem(f'El libro no tiene la hoja requerida «{name}». Selecciona una base generada por la aplicación.', 409)
        sheet = workbook[name]
        if tuple(cell.value for cell in sheet[1]) != headers or table_name not in sheet.tables:
            raise Problem(f'La estructura de «{name}» es incompatible. Se conservó el libro existente.', 409)


def write_export(path, turn):
    path = Path(path)
    if path.suffix.lower() != '.xlsx':
        raise Problem('El destino debe ser un archivo .xlsx.')
    if not path.parent.is_dir():
        raise Problem('La carpeta de destino no existe.')
    temporary = None
    workbook = None
    try:
        if path.exists():
            workbook = load_workbook(path)
            verify_schema(workbook)
        else:
            workbook = Workbook()
            workbook.remove(workbook.active)
            for name in SHEETS:
                workbook.create_sheet(name)
        for name, rows in export_rows(turn).items():
            table_name, headers = SHEETS[name]
            replace_sheet_rows(workbook[name], table_name, headers, turn['id'], rows)
        # Guarda y verifica un libro temporal antes de sustituir la base histórica.
        with NamedTemporaryFile(dir=path.parent, suffix='.xlsx', delete=False) as handle:
            temporary = handle.name
        workbook.save(temporary)
        check = load_workbook(temporary)
        try:
            verify_schema(check)
        finally:
            check.close()
        os.replace(temporary, path)
        temporary = None
    except (PermissionError, OSError) as exc:
        raise Problem('No se pudo escribir el Excel. Si está abierto, ciérralo y vuelve a exportar. El registro local se conservó.', 409) from exc
    except (BadZipFile, InvalidFileException, KeyError, ValueError, SyntaxError, ParseError) as exc:
        raise Problem('El archivo de destino no es un Excel compatible. Se conservó el archivo existente.', 409) from exc
    finally:
        if workbook is not None:
            workbook.close()
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
