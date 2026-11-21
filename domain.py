# Reglas para identificar turnos, validar horarios y comparar lo programado
# con lo ejecutado. Estas funciones operan sobre los datos del turno en memoria.
from datetime import date, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo


# Las horas se representan en minutos desde la medianoche de la fecha operativa.
LEVELS = ('NP', 'NH', 'INY', 'EXT', 'ACA')
ZONE = ZoneInfo('America/Santiago')
SLOT = 15


class Problem(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def clean(value):
    return str(value).strip() if value is not None else ''


def required(value, label):
    value = clean(value)
    if not value:
        raise Problem(f'Completa {label}.')
    return value


# Las mismas claves de un registro importado producen el mismo identificador.
def entity_id(*parts):
    return uuid5(NAMESPACE_URL, repr(parts)).hex


# Un turno queda identificado por su fecha operativa y por el tipo Día o Noche.
def turn_id(day, kind):
    try:
        day = date.fromisoformat(day).isoformat()
    except (TypeError, ValueError) as exc:
        raise Problem('Indica una fecha operativa válida.') from exc
    if kind not in ('Día', 'Noche'):
        raise Problem('Selecciona Día o Noche.')
    return f'{day}|{kind}'


# A partir del minuto 1440, el horario se convierte a una fecha del día siguiente.
def wall_datetime(day, minute):
    return datetime.fromisoformat(day) + timedelta(minutes=minute)


# Convierte la hora actual en Santiago a minutos de la misma escala que el Plan.
def now_minute(day, now=None):
    local = (now or datetime.now(ZONE)).astimezone(ZONE).replace(tzinfo=None)
    return int((local - datetime.fromisoformat(day)).total_seconds() // 60)


# Los intervalos incluyen el inicio y excluyen el término, así que dos bloques contiguos son válidos.
def overlap(first, start, end):
    return first['start'] < end and start < first['end']


def codes(values):
    if not isinstance(values, list):
        raise Problem('Los recursos deben enviarse como lista.')
    # Elimina duplicados y conserva el orden de ingreso.
    return list(dict.fromkeys(code for value in values if (code := clean(value))))


def range_of(turn, payload):
    start, end = payload.get('start'), payload.get('end')
    if type(start) is not int or type(end) is not int:
        raise Problem('Selecciona horas de inicio y término.')
    if not (turn['start'] <= start < end <= turn['end']):
        raise Problem('El rango debe estar dentro del turno y su término debe ser posterior al inicio.')
    # Ambos extremos deben coincidir con una división de la grilla del turno.
    if (start - turn['start']) % SLOT or (end - turn['start']) % SLOT:
        raise Problem('Usa intervalos de 15 minutos.')
    return start, end


def plan_identity(front):
    return tuple(front[field].casefold() for field in ('level', 'block', 'name'))


def assemble_turn(preview, day, kind, previous=None):
    key = turn_id(day, kind)
    if preview['kind'] != kind:
        raise Problem('El tipo seleccionado debe coincidir con el programa del POD.')
    # Se concilian los frentes por nivel, macrobloque y nombre al reemplazar el Plan.
    old_fronts = {plan_identity(front): front for front in (previous or {}).get('fronts', [])}
    fronts = []
    for source in preview['fronts']:
        identity = plan_identity(source)
        old = old_fronts.pop(identity, {})
        front_key = old.get('id') or entity_id('front', key, *identity)
        fronts.append({
            **source, 'id': front_key, 'absent': False,
            'plan': [
                {**activity, 'id': entity_id('plan', front_key, activity['code'], activity['start'])}
                for activity in source['plan']
            ],
            'real': old.get('real', []), 'cnc': old.get('cnc', []),
            'dismissed': old.get('dismissed', {}), 'closing': old.get('closing', ''),
        })
    # Un frente omitido conserva su historial si tiene Real, CNC o un cierre registrado.
    for old in old_fronts.values():
        if old['real'] or old['cnc'] or old['closing']:
            fronts.append({**old, 'plan': [], 'assignments': [], 'expected': '', 'absent': True})
    # Rechaza el reemplazo si sus límites dejarían fuera datos que ya estaban guardados.
    for front in fronts:
        for activity in front['real'] + front['cnc']:
            if activity['start'] < preview['start'] or activity['end'] > preview['end']:
                raise Problem('El nuevo horario deja datos registrados fuera del turno. Se conservó el turno anterior.')
    return {
        'id': key, 'day': key.split('|')[0], 'kind': kind,
        'start': preview['start'], 'end': preview['end'], 'fronts': fronts,
        'source': preview['source'],
    }


def slot_state(front, minute, elapsed):
    # Una diferencia solo se revisa después de que termina el intervalo completo.
    if minute + SLOT > elapsed:
        return None
    expected = next((row for row in front['plan'] if overlap(row, minute, minute + SLOT)), None)
    if expected is None:
        return None
    actual = sorted({row['code'] for row in front['real'] if overlap(row, minute, minute + SLOT)})
    # Basta con que una de las actividades reales coincida con la sigla esperada.
    if expected['code'] in actual:
        return None
    if any(overlap(row, minute, minute + SLOT) for row in front['cnc']):
        return None
    # Un descarte aplica a esta combinación de Plan y Real. Si cambia, se vuelve a evaluar.
    signature = [expected['code'], actual]
    if front['dismissed'].get(str(minute)) == signature:
        return None
    return {'start': minute, 'end': minute + SLOT, 'expected': expected['code'],
            'actual': actual, 'signature': signature}


def suggestions(turn, now=None):
    elapsed = now_minute(turn['day'], now)
    return {
        front['id']: [
            state for minute in range(turn['start'], turn['end'], SLOT)
            if (state := slot_state(front, minute, elapsed))
        ]
        for front in turn['fronts']
    }
