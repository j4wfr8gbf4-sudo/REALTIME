'use strict';

// El mismo tablero sirve para el turno de trabajo y el historial.
// La interfaz pide los datos a la API y vuelve a dibujar el turno tras guardar.
const element = id => document.getElementById(id);
const historyView = window.location.pathname === '/historial';
// El servidor guarda los turnos. Este estado contiene la vista activa y sus controles.
const state = {
  turn: null, suggestions: {}, turns: [], catalog: [], exportPath: '',
  levels: new Set(), busy: false, historyEditing: false, turnRequest: 0,
};
const dialog = element('dialog');
const formBody = element('dialog-body');
const board = element('board');
const grilla = element('board-scroller');
const SLOT = 15;
const fieldLabel = {plan: 'Plan', real: 'Real'};
const quickForm = element('quick-activity');
const quickCode = element('quick-code');
const quickError = element('quick-error');
const quickFields = [
  {key: 'code', label: 'Actividad'},
  {key: 'crew', label: 'Cuadrilla'},
  {key: 'equipment', label: 'Equipo'},
];
let selection = null;
let gesture = null;

function canEdit() {
  return !historyView || state.historyEditing;
}

// Escapa el texto del POD y de los formularios antes de insertarlo en una plantilla HTML.
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'})[char]);
}

function status(message, error = false) {
  const region = element('status');
  region.textContent = message;
  region.classList.toggle('error', error);
}

function modalError(message) {
  const region = element('dialog-error');
  region.hidden = !message;
  region.textContent = message;
}

async function request(path, options = {}) {
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(result.error || 'No se pudo completar la operación.');
    error.status = response.status;
    throw error;
  }
  return result;
}

function post(path, payload) {
  return request(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
}

// Muestra la hora del reloj sin alterar los minutos operativos usados para ordenar el turno.
function hour(minute) {
  const withinDay = minute % 1440;
  return String(Math.floor(withinDay / 60)).padStart(2, '0') + ':' + String(withinDay % 60).padStart(2, '0');
}

function dateLabel(day) {
  return day.split('-').reverse().join('-');
}

function rangeLabel(start, end) {
  return hour(start) + '–' + hour(end) + (end >= 1440 && start < 1440 ? ' (+1 día)' : '');
}

// Convierte los minutos del turno en posición y ancho relativos a la fila del tablero.
function coordinates(start, end) {
  const scale = 100 / (state.turn.end - state.turn.start);
  return `left:${(start - state.turn.start) * scale}%;width:${(end - start) * scale}%`;
}

function overlaps(row, start, end) {
  return row.start < end && start < row.end;
}

function button(label, action, id = '', kind = 'button', extra = '') {
  return `<button class="${kind}" type="button" data-click="${action}" data-id="${escapeHtml(id)}" ${extra}>${label}</button>`;
}

function boardBlock(label, action, id, kind, extra) {
  return canEdit() ? button(label, action, id, kind, extra) : `<div class="${kind}" ${extra}>${label}</div>`;
}

function setupTurn(result, message) {
  state.turn = result.turn;
  state.suggestions = result.suggestions;
  if (!state.turns.some(item => item.id === result.turn.id)) {
    state.turns.unshift(result.turn);
    state.turns.sort((a, b) => b.id.localeCompare(a.id));
  }
  render();
  if (message) status(message);
}

async function loadTurn(id) {
  if (!id) return;
  // Si cambia el turno mientras se carga, solo se muestra la respuesta de la última solicitud.
  const sequence = ++state.turnRequest;
  const toggle = element('history-toggle');
  toggle.disabled = true;
  try {
    const result = await request('/api/turn?id=' + encodeURIComponent(id));
    if (sequence === state.turnRequest) {
      setupTurn(result);
      return true;
    }
  } catch (error) {
    if (sequence === state.turnRequest) {
      render();
      throw error;
    }
  }
}

function render() {
  const active = state.turn;
  element('turn-select').innerHTML = state.turns.length ? state.turns.map(item =>
    `<option value="${escapeHtml(item.id)}" ${item.id === active?.id ? 'selected' : ''}>${dateLabel(item.day)} · ${item.kind}</option>`
  ).join('') : '<option>Sin turnos guardados</option>';
  element('turn-select').disabled = !state.turns.length;
  element('filters').hidden = !active;
  element('export-button').disabled = !active;
  element('history-toggle').disabled = !active;
  element('history-toggle').textContent = state.historyEditing ? 'Volver a solo lectura' : 'Activar edición';
  element('history-mode').textContent = state.historyEditing ? 'Modo editor' : 'Solo lectura';
  board.classList.toggle('readonly', !canEdit());
  element('empty').hidden = Boolean(active);
  grilla.hidden = !active;
  if (!active) return;
  element('turn-window').textContent = rangeLabel(active.start, active.end);
  element('turn-source').textContent = active.source;
  renderBoard();
}

// Los filtros cambian las filas visibles. El turno permanece completo en memoria.
function renderBoard() {
  if (!state.turn) return;
  clearSelection();
  clearGesture();
  const {start, end, fronts} = state.turn;
  const available = [...new Set(['NP', 'NH', 'INY', 'EXT', 'ACA', ...fronts.map(front => front.level)])];
  element('level-buttons').innerHTML = ['Todos', ...available].map(level =>
    button(escapeHtml(level), 'level', level, 'level', `aria-pressed="${level === 'Todos' ? !state.levels.size : state.levels.has(level)}"`)
  ).join('');
  const search = element('front-search').value.trim().toLocaleLowerCase('es-CL');
  const onlySp = element('only-sp').checked;
  const includeSp = element('show-sp').checked;
  const filtered = fronts.filter(front => {
    const matchesLevel = !state.levels.size || state.levels.has(front.level);
    const matchesText = [front.name, front.block].some(value => value.toLocaleLowerCase('es-CL').includes(search));
    const matchesSp = onlySp ? !front.plan.length : (includeSp || front.plan.length || front.real.length || front.absent || front.cnc.length);
    return matchesLevel && matchesText && matchesSp;
  });
  const hours = [];
  for (let minute = start; minute <= end; minute += 60) {
    const position = (minute - start) * 100 / (end - start);
    hours.push(`<span style="left:${position}%" class="${minute === end ? 'last' : ''}" title="${minute >= 1440 ? 'Día siguiente' : 'Fecha operativa'}">${hour(minute)}</span>`);
  }
  board.innerHTML = `<div class="board-head">
    <div class="heading">Frente · ${filtered.length} de ${fronts.length}</div>
    <div class="hours">${hours.join('')}</div>
    <div class="heading">Cierre de turno</div>
  </div>${filtered.length ? filtered.map(renderFront).join('') : '<p class="empty-filter">No hay frentes para los filtros seleccionados.</p>'}`;
}

function blockColor(code) {
  const fixed = {EXT:'#b8dfb8', AC:'#b8dfb8', LP1:'#b8dfb8', LP2:'#b8dfb8',
    PP:'#f5c18e', PF:'#c5d6f3', AD:'#a8cce9', SH:'#b6dce8', TRAS:'#dce1e7', RM:'#cfdfec'};
  if (Object.hasOwn(fixed, code)) return fixed[code];
  if (code.startsWith('TOP')) return '#f5c18e';
  return '#dce7f3';
}

// En Real, los recursos pertenecen a la actividad. En Plan se consultan por sus intervalos.
function activityResources(front, field, row) {
  if (field === 'real') return {equipment: row.equipment, crew: row.crew};
  const assignments = front.assignments.filter(item => overlaps(item, row.start, row.end));
  const ofType = type => [...new Set(assignments.filter(item => item.type === type).map(item => item.code))];
  return {equipment: ofType('Equipo'), crew: ofType('Cuadrilla')};
}

// La diferencia de recursos se marca en el tramo que coincide con una actividad del mismo código.
function hasAssignmentDifference(front, real) {
  const matches = front.plan.filter(plan => plan.code === real.code && overlaps(plan, real.start, real.end));
  if (!matches.length) return false;
  for (const plan of matches) {
    for (let minute = Math.max(real.start, plan.start); minute < Math.min(real.end, plan.end); minute += SLOT) {
      const assignments = front.assignments.filter(row => overlaps(row, minute, minute + SLOT));
      const compare = (kind, actual) => {
        const planned = [...new Set(assignments.filter(row => row.type === kind).map(row => row.code))].sort();
        return JSON.stringify(planned) !== JSON.stringify([...actual].sort());
      };
      if (compare('Equipo', real.equipment) || compare('Cuadrilla', real.crew)) return true;
    }
  }
  return false;
}

function renderActivityLane(front, field, activities, index = 0) {
  const unit = SLOT * 100 / (state.turn.end - state.turn.start);
  const label = field === 'plan' ? 'PLAN' : index ? 'REAL SIM.' : 'REAL';
  const blocks = activities.map(row => {
    const {equipment, crew} = activityResources(front, field, row);
    const different = field === 'real' && hasAssignmentDifference(front, row);
    const title = [row.code, rangeLabel(row.start, row.end),
      equipment.length && 'Equipo: ' + equipment.join(', '),
      crew.length && 'Cuadrilla: ' + crew.join(', '),
      different && 'Asignación distinta al Plan',
      canEdit() && 'Doble clic para editar · Ctrl + arrastrar un borde para ajustar la duración'].filter(Boolean).join(' · ');
    const resources = [...crew, ...equipment].join(' · ');
    return boardBlock(`<span>${escapeHtml(row.code)}</span>${resources ? `<small>${escapeHtml(resources)}</small>` : ''}`,
      'activity', row.id, different ? 'block activity diff' : 'block activity',
      `style="${coordinates(row.start, row.end)};background:${blockColor(row.code)}" title="${escapeHtml(title)}" aria-label="${escapeHtml(title)}"`);
  }).join('');
  return `<div class="lane-wrap"><span class="lane-label">${label}</span><div class="lane ${field}" style="--unit:${unit}%" ${canEdit() ? 'tabindex="0"' : ''} role="group" aria-label="${label}, ${escapeHtml(front.name)}" data-grid="${field}">
    ${blocks}</div></div>`;
}

// Cada fila admite bloques sin solapamiento. Las actividades simultáneas ocupan filas adicionales.
function renderReal(front) {
  const lanes = [[]];
  for (const activity of front.real) {
    const lane = lanes.find(items => !items.some(item => overlaps(item, activity.start, activity.end)));
    if (lane) lane.push(activity);
    else lanes.push([activity]);
  }
  return lanes.map((lane, index) => renderActivityLane(front, 'real', lane, index)).join('');
}

function renderReview(front) {
  const pending = state.suggestions[front.id] || [];
  if (!pending.length && !front.cnc.length) return '';
  // Agrupa sugerencias contiguas cuando describen la misma diferencia entre Plan y Real.
  const combine = [];
  for (const slot of pending) {
    const last = combine.at(-1);
    if (last && last.end === slot.start && JSON.stringify(last.signature) === JSON.stringify(slot.signature)) {
      last.end = slot.end;
    } else {
      combine.push({...slot});
    }
  }
  const buttons = combine.map(item => {
    const title = (canEdit() ? 'Revisar ' : 'Pendiente de revisión ') + rangeLabel(item.start, item.end) + ' · Plan ' + item.expected + ' · ' +
      (item.actual.length ? 'Real ' + item.actual.join(', ') : 'Sin registro real');
    return boardBlock('?', 'review', item.start + ',' + item.end, 'block pending',
      `title="${escapeHtml(title)}" aria-label="${escapeHtml(title)}" style="${coordinates(item.start, item.end)}"`);
  }).concat(front.cnc.map(item => {
    const title = 'CNC ' + item.code + ' · ' + item.primary + ' · ' + item.secondary;
    return boardBlock(escapeHtml(item.code), 'edit-cnc', item.id, 'block confirmed',
      `title="${escapeHtml(title)}" aria-label="${escapeHtml(title)}" style="${coordinates(item.start, item.end)}"`);
  }));
  return `<div class="lane-wrap"><span class="lane-label">CNC</span><div class="lane review" style="--unit:${1500 / (state.turn.end - state.turn.start)}%">
    ${buttons.join('')}</div></div>`;
}

function renderFront(front) {
  const showReal = element('show-real').checked;
  const showPlan = element('show-plan').checked;
  const tag = front.absent ? '<span class="tag sp">Fuera del POD</span>' :
    (!front.plan.length ? '<span class="tag sp">SP</span>' : '');
  return `<article class="front" data-front="${front.id}">
    <div class="info">
      <div class="front-title"><span class="tag">${escapeHtml(front.level)}</span><span class="tag">${escapeHtml(front.block)}</span>${tag}
      <strong>${escapeHtml(front.name)}</strong></div>
      <span class="caption">Inicio de turno</span><p class="detail">${escapeHtml(front.opening || '—')}</p>
      ${showReal && canEdit() ? button('+ Registrar Real', 'new-real', '', 'inline') : ''}
    </div>
    <div class="timeline">${showPlan ? renderActivityLane(front, 'plan', front.plan) : ''}${showReal ? renderReal(front) + renderReview(front) : ''}</div>
    <div class="closing">
      <span class="caption">Esperado</span><p class="detail">${escapeHtml(front.expected || '—')}</p>
      <span class="caption">Real</span>${canEdit() ? button(escapeHtml(front.closing || 'Registrar cierre'), 'closing', '', 'inline') : `<p class="detail">${escapeHtml(front.closing || '—')}</p>`}
    </div>
  </article>`;
}

function openModal(title, content) {
  clearSelection();
  element('dialog-title').textContent = title;
  formBody.innerHTML = content;
  modalError('');
  if (!dialog.open) dialog.showModal();
}

// Bloquea envíos simultáneos y muestra los errores del servidor dentro del diálogo.
function submitForm(action) {
  const form = formBody.querySelector('form');
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (state.busy) return;
    state.busy = true;
    const trigger = event.submitter;
    if (trigger) trigger.disabled = true;
    modalError('');
    try {
      await action(new FormData(form));
    } catch (error) {
      modalError(error.message);
    } finally {
      state.busy = false;
      if (trigger?.isConnected) trigger.disabled = false;
    }
  });
}

// La etiqueta muestra la hora local. El valor enviado conserva los minutos operativos.
function timeSelect(name, label, value) {
  const options = [];
  for (let minute = state.turn.start; minute <= state.turn.end; minute += SLOT) {
    options.push(`<option value="${minute}" ${minute === value ? 'selected' : ''}>${hour(minute)}${minute >= 1440 ? ' (+1 día)' : ''}</option>`);
  }
  return `<label>${label}<select name="${name}">${options.join('')}</select></label>`;
}

function actionButton(label, classes = 'button primary') {
  return `<button type="submit" class="${classes}">${label}</button>`;
}

async function updateFront(front, action, values, message) {
  await saveOnBoard(front, action, values, message);
  if (dialog.open) dialog.close();
}

function editableRange(start, end) {
  return `<div class="pair">${timeSelect('start', 'Inicio', start)}${timeSelect('end', 'Término', end)}</div>`;
}

function removeEntry(front, field, entry) {
  const label = field === 'cnc' ? 'CNC ' + entry.code : 'actividad de ' + fieldLabel[field] + ' ' + entry.code;
  openModal('Eliminar ' + label, `<form class="form"><p class="detail">${escapeHtml(front.name)} · ${rangeLabel(entry.start, entry.end)}</p>
    <div class="actions">${actionButton('Confirmar eliminación', 'button danger')}</div></form>`);
  submitForm(() => updateFront(front, 'remove_' + field, {id: entry.id}, 'Registro eliminado.'));
}

// Comparte los campos de Plan y Real. Al editar se mantiene el ID del bloque.
function activityForm(front, field, options = {}) {
  const start = options.start ?? state.turn.start;
  const end = options.end ?? Math.min(start + SLOT, state.turn.end);
  const entry = options.entry;
  const defaults = [...new Set(front.plan.map(row => row.code))];
  const planned = front.plan.find(row => overlaps(row, start, end));
  const resources = entry ? activityResources(front, field, entry) : {equipment: [], crew: []};
  const real = field === 'real';
  openModal((entry ? 'Editar ' : 'Registrar ') + fieldLabel[field] + ' · ' + front.name, `<form class="form">
    ${editableRange(start, end)}
    <label>Sigla de actividad<input name="code" list="activity-options" value="${escapeHtml(entry?.code || (real && options.start !== undefined ? planned?.code : ''))}" required></label>
    <datalist id="activity-options">${defaults.map(code => `<option value="${escapeHtml(code)}"></option>`).join('')}</datalist>
    <div class="pair"><label>Equipos<input name="equipment" placeholder="Separados por coma" value="${escapeHtml(resources.equipment.join(', '))}"></label>
    <label>Cuadrillas<input name="crew" placeholder="Separadas por coma" value="${escapeHtml(resources.crew.join(', '))}"></label></div>
    ${real ? `<label class="check"><input type="checkbox" name="simultaneous" ${options.simultaneous ? 'checked' : ''}> Permitir actividad simultánea</label>` : ''}
    <div class="actions">${actionButton('Guardar actividad')}
      ${real && entry ? button('Agregar simultánea', 'entry-simultaneous', '', 'button') : ''}
      ${entry ? button('Eliminar', 'remove', '', 'button danger') : ''}
    </div>
  </form>`);
  submitForm(values => updateFront(front, field, {
    id: entry?.id, start: Number(values.get('start')), end: Number(values.get('end')), code: values.get('code'),
    equipment: values.get('equipment').split(','), crew: values.get('crew').split(','),
    simultaneous: values.has('simultaneous'),
  }, 'Actividad de ' + fieldLabel[field] + ' guardada.'));
  formBody.querySelector('[data-click="entry-simultaneous"]')?.addEventListener('click', () =>
    activityForm(front, field, {start, end, simultaneous: true}));
  formBody.querySelector('[data-click="remove"]')?.addEventListener('click', () => removeEntry(front, field, entry));
}

// Desde una sugerencia se puede registrar el Real, descartar la diferencia o confirmar una CNC.
function cncForm(front, options) {
  const {start, end, entry} = options;
  const selected = entry?.catalog;
  const label = item => escapeHtml(item.code + ' · ' + item.primary + ' · ' + item.secondary);
  const entries = state.catalog.filter(item => item.id !== selected).map(item =>
    `<option value="${item.id}">${label(item)}</option>`).join('');
  // Una CNC existente muestra las clasificaciones guardadas con el registro.
  const original = entry ? `<option selected value="${escapeHtml(selected)}">${label(entry)} (registrada)</option>` : '';
  const hasCatalog = state.catalog.length || entry;
  const detail = options.detail ? `<p class="detail">${escapeHtml(options.detail)}</p>` : '';
  openModal((entry ? 'Editar CNC' : 'Revisar diferencia') + ' · ' + front.name, `<form class="form">
    ${detail}${editableRange(start, end)}
    ${hasCatalog ? `<label>Código · clasificación primaria · secundaria
      <select name="catalog">${original || '<option value="">Selecciona una combinación</option>'}${entries}</select></label>
      <label>Observación<textarea name="note">${escapeHtml(entry?.note)}</textarea></label>` :
      '<p class="detail">Agrega una combinación en el catálogo para confirmar una CNC.</p>'}
    <div class="actions">
      ${hasCatalog ? actionButton(entry ? 'Guardar cambios' : 'Confirmar CNC') : button('Abrir catálogo', 'catalog')}
      ${entry ? button('Eliminar', 'remove', '', 'button danger') :
      button('Registrar Real', 'log-real') + button('Descartar sugerencia', 'dismiss')}
    </div></form>`);
  submitForm(values => updateFront(front, 'cnc', {id: entry?.id, start: Number(values.get('start')),
    end: Number(values.get('end')), catalog: values.get('catalog'), note: values.get('note')}, 'CNC guardada.'));
  formBody.querySelector('[data-click="catalog"]')?.addEventListener('click', () => catalogForm(null, () => cncForm(front, options)));
  formBody.querySelector('[data-click="log-real"]')?.addEventListener('click', () => activityForm(front, 'real', {start, end}));
  formBody.querySelector('[data-click="dismiss"]')?.addEventListener('click', () =>
    updateFront(front, 'dismiss', {start, end}, 'Sugerencia revisada.').catch(error => modalError(error.message)));
  formBody.querySelector('[data-click="remove"]')?.addEventListener('click', () => removeEntry(front, 'cnc', entry));
}

function closingForm(front) {
  openModal('Estado real al cierre · ' + front.name, `<form class="form">
    <label>Estado del frente<textarea name="note">${escapeHtml(front.closing)}</textarea></label>
    <div class="actions">${actionButton('Guardar cierre')}</div></form>`);
  submitForm(values => updateFront(front, 'closing', {note: values.get('note')}, 'Estado de cierre guardado.'));
}

function catalogForm(entry, back) {
  const rows = state.catalog.map(item =>
    `<li>${button(escapeHtml(item.code + ' · ' + item.primary + ' · ' + item.secondary), 'catalog-edit', item.id, 'inline')}</li>`
  ).join('');
  openModal('Catálogo CNC', `<form class="form">
    ${rows ? `<ul class="catalog-list">${rows}</ul>` : '<p class="detail">El catálogo está vacío.</p>'}
    <label>Código<input name="code" value="${escapeHtml(entry?.code)}" required></label>
    <label>Clasificación primaria<input name="primary" value="${escapeHtml(entry?.primary)}" required></label>
    <label>Clasificación secundaria<input name="secondary" value="${escapeHtml(entry?.secondary)}" required></label>
    <div class="actions">${actionButton(entry ? 'Actualizar combinación' : 'Agregar combinación')}
    ${entry ? button('Nueva combinación', 'catalog-new') : ''}${back ? button('Volver a la CNC', 'back') : ''}</div>
  </form>`);
  submitForm(async values => {
    const result = await post('/api/catalog', {id: entry?.id, ...Object.fromEntries(values)});
    state.catalog = result.catalog;
    status('Catálogo actualizado.');
    catalogForm(null, back);
  });
  for (const item of formBody.querySelectorAll('[data-click="catalog-edit"]')) {
    item.addEventListener('click', () => catalogForm(state.catalog.find(row => row.id === item.dataset.id), back));
  }
  formBody.querySelector('[data-click="catalog-new"]')?.addEventListener('click', () => catalogForm(null, back));
  formBody.querySelector('[data-click="back"]')?.addEventListener('click', back);
}

// El token enlaza la confirmación del usuario con el contenido que el servidor ya leyó.
function displayPreview(preview, existingRequest = null) {
  const valid = preview.fronts.length;
  const programmed = preview.fronts.filter(front => front.plan.length).length;
  const exceptions = preview.warnings.map(warning => `<li>${escapeHtml(warning)}</li>`).join('');
  const previewRows = preview.fronts.slice(0, 12).map(front =>
    `<li>${escapeHtml(front.level + ' · ' + front.block + ' · ' + front.name + ' · ' +
       (front.plan.length ? front.plan.length + ' actividades' : 'Sin programa'))}</li>`
  ).join('');
  openModal(existingRequest ? 'Reemplazar Plan del turno' : 'Revisar importación', `<form class="form">
    <div class="pair">
      <label>Fecha operativa<input type="date" name="day" value="${escapeHtml(existingRequest?.day || preview.day)}" required ${existingRequest ? 'readonly' : ''}></label>
      <label>Turno<input value="${escapeHtml(preview.kind)}" readonly></label>
    </div>
    <p class="detail">${valid} frentes · ${programmed} con Plan · ${rangeLabel(preview.start, preview.end)}</p>
    ${existingRequest ? `<p class="detail">${escapeHtml(existingRequest.message)} El Real, los cierres reales y las CNC se conservarán.</p>` :
      `<details open><summary>Frentes detectados ${valid > 12 ? '(primeros 12)' : ''}</summary><ul class="preview">${previewRows}</ul></details>`}
    ${exceptions ? `<details open><summary>Incidencias (${preview.warnings.length})</summary><ul class="preview">${exceptions}</ul></details>` : ''}
    <div class="actions">${actionButton(existingRequest ? 'Confirmar reemplazo' : 'Cargar Plan')}</div>
  </form>`);
  submitForm(async values => {
    const day = values.get('day');
    try {
      const result = await post('/api/import', {token: preview.token, day, kind: preview.kind, replace: Boolean(existingRequest)});
      dialog.close();
      state.levels.clear();
      element('front-search').value = '';
      setupTurn(result, 'Plan cargado.');
    } catch (error) {
      if (error.status === 409 && !existingRequest) {
        displayPreview(preview, {day, message: error.message});
        return;
      }
      throw error;
    }
  });
}

function exportForm() {
  openModal('Actualizar base histórica', `<form class="form">
    <p class="detail">${dateLabel(state.turn.day)} · ${escapeHtml(state.turn.kind)}</p>
    <label>Ruta del archivo Excel<input name="path" value="${escapeHtml(state.exportPath)}" required></label>
    <p class="muted">El archivo se creará si falta. Si este turno ya existe en la base, sus filas se actualizarán.</p>
    <div class="actions">${actionButton('Exportar turno')}</div>
  </form>`);
  submitForm(async values => {
    const result = await post('/api/export', {turn: state.turn.id, path: values.get('path')});
    state.exportPath = result.path;
    dialog.close();
    status('Base actualizada en ' + result.path);
  });
}

function frontFor(node) {
  return state.turn?.fronts.find(front => front.id === node.closest('[data-front]')?.dataset.front);
}

// Delega las acciones al tablero porque sus filas se reconstruyen al renderizar.
board.addEventListener('click', event => {
  if (!canEdit()) return;
  const control = event.target.closest('[data-click]');
  if (!control) return;
  const {click, id} = control.dataset;
  const front = frontFor(control);
  if (!front) return;
  if (click === 'new-real') activityForm(front, 'real');
  if (click === 'closing') closingForm(front);
  if (click === 'activity' && event.detail === 0) editActivity(control);
  if (click === 'edit-cnc') {
    const entry = front.cnc.find(item => item.id === id);
    if (entry) cncForm(front, {...entry, entry});
  }
  if (click === 'review') {
    const [start, end] = id.split(',').map(Number);
    const slot = state.suggestions[front.id].find(item => item.start === start);
    const detail = 'Plan ' + slot.expected + ' · ' + (slot.actual.length ? 'Real ' + slot.actual.join(', ') : 'Sin registro real');
    cncForm(front, {start, end, detail});
  }
});

// Convierte la posición horizontal del puntero en un intervalo de la grilla.
function slotAt(event, lane) {
  const rect = lane.getBoundingClientRect();
  const count = (state.turn.end - state.turn.start) / SLOT;
  const index = Math.floor((event.clientX - rect.left) * count / rect.width);
  return state.turn.start + Math.max(0, Math.min(count - 1, index)) * SLOT;
}

function editActivity(control) {
  const lane = control.closest('[data-grid]');
  const front = frontFor(lane);
  const field = lane.dataset.grid;
  const entry = front?.[field].find(item => item.id === control.dataset.id);
  if (entry) activityForm(front, field, {...entry, entry});
}

board.addEventListener('dblclick', event => {
  if (!canEdit() || state.busy) return;
  const control = event.target.closest('[data-click="activity"]');
  if (control && !event.shiftKey && !event.ctrlKey) editActivity(control);
});

function markRange(lane, name) {
  const mark = document.createElement('div');
  mark.className = name;
  mark.setAttribute('aria-hidden', 'true');
  lane.append(mark);
  return mark;
}

function closeQuickEntry() {
  quickForm.hidden = true;
  quickError.textContent = '';
}

function clearSelection() {
  selection?.mark.remove();
  selection = null;
  closeQuickEntry();
}

function extendSelection(target) {
  selection.target = target;
  selection.start = Math.min(selection.anchorStart, target);
  selection.end = Math.max(selection.anchorEnd, target + SLOT);
  selection.mark.style.cssText = coordinates(selection.start, selection.end);
}

// El ancla define el rango original. El borrador permite recorrer los campos antes de guardar.
function selectRange(lane, start, entry = null) {
  clearSelection();
  const front = frontFor(lane);
  const field = lane.dataset.grid;
  const resources = entry ? activityResources(front, field, entry) : {crew: [], equipment: []};
  const initial = {code: entry?.code ?? '', crew: resources.crew.join(', '), equipment: resources.equipment.join(', ')};
  selection = {lane, front, field, entry, initial, draft: {...initial}, quickIndex: 0,
    anchorStart: entry?.start ?? start, anchorEnd: entry?.end ?? start + SLOT,
    mark: markRange(lane, 'selection')};
  extendSelection(start);
}

function positionQuickEntry() {
  if (quickForm.hidden || !selection) return;
  const {lane, mark} = selection;
  const rect = mark.getBoundingClientRect();
  const scroller = grilla.getBoundingClientRect();
  const left = Math.max(scroller.left + 8, Math.min(rect.left, window.innerWidth - quickForm.offsetWidth - 8));
  const roomBelow = window.innerHeight - lane.getBoundingClientRect().bottom;
  const top = roomBelow >= quickForm.offsetHeight + 5 ? rect.bottom + 5 : rect.top - quickForm.offsetHeight - 5;
  quickForm.style.left = Math.max(8, Math.min(left, window.innerWidth - quickForm.offsetWidth - 8)) + 'px';
  quickForm.style.top = Math.max(8, Math.min(top, window.innerHeight - quickForm.offsetHeight - 8)) + 'px';
}

function openQuickEntry(firstLetter) {
  const {key, label} = quickFields[selection.quickIndex];
  element('quick-field').textContent = label;
  quickError.textContent = '';
  quickCode.value = firstLetter ?? selection.draft[key];
  quickForm.hidden = false;
  positionQuickEntry();
  quickCode.focus({preventScroll: true});
  if (firstLetter === undefined) quickCode.select();
}

function rememberQuickEntry() {
  if (!quickForm.hidden) selection.draft[quickFields[selection.quickIndex].key] = quickCode.value.trim();
}

function cycleQuickField(event) {
  event.preventDefault();
  rememberQuickEntry();
  const step = event.shiftKey ? -1 : 1;
  selection.quickIndex = (selection.quickIndex + step + quickFields.length) % quickFields.length;
  openQuickEntry();
}

async function saveOnBoard(front, action, values, message) {
  if (!canEdit()) return;
  const turn = state.turn.id;
  const sequence = state.turnRequest;
  const result = await post('/api/front', {turn, front: front.id, action, ...values});
  // Aplica la respuesta al tablero solo si sigue mostrando la vista que inició el guardado.
  if (state.turn.id === turn && sequence === state.turnRequest && canEdit()) {
    setupTurn(result, message);
    return result;
  }
}

quickForm.addEventListener('submit', async event => {
  event.preventDefault();
  if (!canEdit() || state.busy || !selection) return;
  rememberQuickEntry();
  const {front, field, entry, start, end, initial, draft, quickIndex} = selection;
  if (!draft.code) {
    quickError.textContent = 'Ingresa la sigla en el campo Actividad para guardar.';
    positionQuickEntry();
    return;
  }
  // Envía solo los cambios y agrega el rango original si la edición modifica sus límites.
  const values = {id: entry?.id};
  if (!entry || entry.start !== start || entry.end !== end) {
    Object.assign(values, {start, end});
    if (entry) values.original_range = [entry.start, entry.end];
  }
  for (const {key} of quickFields) {
    if (draft[key] !== initial[key]) values[key] = key === 'code' ? draft[key] : draft[key].split(',');
  }
  state.busy = true;
  quickCode.readOnly = true;
  quickError.textContent = '';
  try {
    const result = await saveOnBoard(front, field, values, 'Actividad guardada en ' + fieldLabel[field] + '.');
    if (result) {
      const savedFront = result.turn.fronts.find(row => row.id === front.id);
      const saved = savedFront[field].find(row => entry ? row.id === entry.id : row.start === start && row.end === end);
      const control = saved && [...board.querySelectorAll('[data-click="activity"]')].find(node => node.dataset.id === saved.id);
      if (control) {
        selectRange(control.closest('[data-grid]'), saved.start, saved);
        selection.quickIndex = quickIndex;
        control.focus({preventScroll: true});
      }
    }
  } catch (error) {
    if (!quickForm.hidden) {
      quickError.textContent = error.message;
      positionQuickEntry();
    } else {
      status(error.message, true);
    }
  } finally {
    quickCode.readOnly = false;
    state.busy = false;
  }
});

quickForm.addEventListener('keydown', event => {
  if (state.busy || !selection) return;
  if (event.key === 'Tab') {
    cycleQuickField(event);
    return;
  }
  if (event.key !== 'Escape') return;
  event.preventDefault();
  const lane = selection?.lane;
  clearSelection();
  lane?.focus({preventScroll: true});
});

window.addEventListener('scroll', positionQuickEntry, true);
window.addEventListener('resize', positionQuickEntry);

function clearGesture() {
  gesture?.preview?.remove();
  if (gesture?.edge) gesture.lane.classList.remove('resizing');
  gesture = null;
}

// Redimensionar fija un borde. Mover o copiar conserva la duración y el punto de agarre.
function destinationAt(event, source) {
  if (source.edge) {
    const width = source.lane.getBoundingClientRect().width;
    const count = (state.turn.end - state.turn.start) / SLOT;
    const delta = Math.round((event.clientX - source.x) * count / width) * SLOT;
    const {entry, lane, front, field} = source;
    const start = source.edge === 'start' ? Math.min(entry.start + delta, entry.end - SLOT) : entry.start;
    const end = source.edge === 'end' ? Math.max(entry.end + delta, entry.start + SLOT) : entry.end;
    return {lane, start, end, front, field};
  }
  const lane = document.elementFromPoint(event.clientX, event.clientY)?.closest('[data-grid]');
  if (!lane || !board.contains(lane) || (source.field === 'real' && lane.dataset.grid === 'plan')) return null;
  const start = slotAt(event, lane) - source.offset;
  return {lane, start, end: start + source.entry.end - source.entry.start,
    front: frontFor(lane), field: lane.dataset.grid};
}

board.addEventListener('pointerdown', event => {
  const lane = event.target.closest('[data-grid]');
  if (!canEdit() || !lane || event.button !== 0 || state.busy || gesture) return;
  const start = slotAt(event, lane);
  if (event.shiftKey && selection?.lane === lane) {
    rememberQuickEntry();
    closeQuickEntry();
    extendSelection(start);
    lane.focus({preventScroll: true});
    return;
  }
  const control = event.target.closest('[data-click="activity"]');
  const entry = frontFor(lane)[lane.dataset.grid].find(item => item.id === control?.dataset.id);
  let edge;
  if (entry && event.ctrlKey) {
    const rect = control.getBoundingClientRect();
    edge = event.clientX <= rect.left + rect.width / 2 ? 'start' : 'end';
    lane.classList.add('resizing');
    event.preventDefault();
  }
  selectRange(lane, start, entry);
  (control ?? lane).focus({preventScroll: true});
  // Mantiene los eventos del gesto aunque el puntero salga del bloque seleccionado.
  (control ?? lane).setPointerCapture(event.pointerId);
  gesture = {pointerId: event.pointerId, lane, front: selection.front, field: lane.dataset.grid,
    entry, edge, offset: entry ? start - entry.start : 0, x: event.clientX, y: event.clientY, dragging: false};
});

// Arrastrar sobre un espacio vacío extiende la selección. Sobre un bloque muestra su destino.
board.addEventListener('pointermove', event => {
  if (!gesture || gesture.pointerId !== event.pointerId) return;
  if (!gesture.entry) {
    extendSelection(slotAt(event, gesture.lane));
    return;
  }
  if (!gesture.dragging && Math.hypot(event.clientX - gesture.x, event.clientY - gesture.y) < 5) return;
  gesture.dragging = true;
  const destination = destinationAt(event, gesture);
  if (gesture.preview?.parentElement !== destination?.lane) {
    gesture.preview?.remove();
    gesture.preview = destination ? markRange(destination.lane, 'drop-preview') : null;
  }
  if (destination && gesture.preview) {
    const invalid = destination.start < state.turn.start || destination.end > state.turn.end;
    gesture.preview.style.cssText = coordinates(destination.start, destination.end);
    gesture.preview.classList.toggle('invalid', invalid);
  }
});

// Al soltar se envía la operación. El servidor valida horarios y solapamientos.
board.addEventListener('pointerup', async event => {
  if (!gesture || gesture.pointerId !== event.pointerId) return;
  const source = gesture;
  clearGesture();
  if (!source.entry) {
    extendSelection(slotAt(event, source.lane));
    return;
  }
  if (!source.dragging) return;
  const target = destinationAt(event, source);
  clearSelection();
  if (!target) return;
  if (source.field === target.field && source.front === target.front &&
      source.entry.start === target.start && source.entry.end === target.end) return;
  state.busy = true;
  try {
    if (source.edge) {
      await saveOnBoard(target.front, source.field, {id: source.entry.id, start: target.start, end: target.end,
        original_range: [source.entry.start, source.entry.end]},
        'Duración actualizada en ' + fieldLabel[source.field] + '.');
    } else {
      const copy = source.field !== target.field;
      await saveOnBoard(target.front, 'place_activity', {
        id: source.entry.id, source_front: source.front.id, source_field: source.field,
        destination_field: target.field, start: target.start,
      }, copy ? 'Actividad copiada de Plan a Real.' : 'Actividad movida en ' + fieldLabel[target.field] + '.');
    }
  } catch (error) {
    status(error.message, true);
  } finally {
    state.busy = false;
  }
});

board.addEventListener('pointercancel', () => {
  clearGesture();
  clearSelection();
});

board.addEventListener('keydown', event => {
  const lane = event.target.closest('[data-grid]');
  if (!canEdit() || !lane || state.busy || event.ctrlKey || event.metaKey || event.altKey) return;
  if (event.key === 'Escape' && selection?.lane === lane) {
    clearSelection();
    return;
  }
  if (event.key === 'Enter' && event.target === lane && selection?.lane !== lane) {
    event.preventDefault();
    activityForm(frontFor(lane), lane.dataset.grid);
    return;
  }
  if (selection?.lane !== lane) return;
  if (event.key === 'Tab') {
    cycleQuickField(event);
  } else if (event.shiftKey && ['ArrowRight', 'ArrowLeft'].includes(event.key)) {
    event.preventDefault();
    const next = selection.target + (event.key === 'ArrowRight' ? SLOT : -SLOT);
    extendSelection(Math.max(state.turn.start, Math.min(next, state.turn.end - SLOT)));
  } else if (event.key === 'Enter') {
    event.preventDefault();
    openQuickEntry();
  } else if (/^[\p{L}\p{N}]$/u.test(event.key)) {
    event.preventDefault();
    openQuickEntry(event.key);
  }
});

element('level-buttons').addEventListener('click', event => {
  const level = event.target.closest('[data-click="level"]')?.dataset.id;
  if (!level) return;
  if (level === 'Todos') state.levels.clear();
  else if (state.levels.has(level)) state.levels.delete(level);
  else state.levels.add(level);
  renderBoard();
});
for (const id of ['show-plan', 'show-real', 'show-sp', 'only-sp', 'front-search']) {
  element(id).addEventListener('input', renderBoard);
}
element('turn-select').addEventListener('change', event => {
  if (state.busy) {
    event.target.value = state.turn.id;
    return;
  }
  const id = event.target.value;
  // El permiso de edición del historial se activa de nuevo para cada turno.
  if (state.historyEditing) {
    state.historyEditing = false;
    render();
  }
  state.levels.clear();
  element('front-search').value = '';
  status('');
  loadTurn(id).catch(error => status(error.message, true));
});
element('history-toggle').addEventListener('click', async () => {
  if (!historyView || !state.turn || state.busy) return;
  if (state.historyEditing) {
    state.historyEditing = false;
    render();
    return;
  }
  try {
    if (await loadTurn(state.turn.id)) {
      state.historyEditing = true;
      render();
    }
  } catch (error) {
    status(error.message, true);
  }
});
element('dialog-close').addEventListener('click', () => dialog.close());
element('catalog-button').addEventListener('click', () => catalogForm());
element('export-button').addEventListener('click', exportForm);
element('import-button').addEventListener('click', () => element('pod-file').click());
element('pod-file').addEventListener('change', async event => {
  const file = event.target.files[0];
  event.target.value = '';
  if (!file) return;
  status('Leyendo PROG…');
  try {
    const preview = await request('/api/preview', {
      method: 'POST', headers: {'X-File-Name': encodeURIComponent(file.name)}, body: file,
    });
    status('');
    displayPreview(preview);
  } catch (error) {
    status(error.message, true);
  }
});

// Carga el catálogo y la lista de turnos antes de abrir el registro más reciente.
async function start() {
  if (historyView) {
    document.title = 'Historial de turnos';
    element('page-title').textContent = 'Historial de turnos';
    element('work-toolbar').hidden = true;
    element('history-toolbar').hidden = false;
    element('empty').innerHTML = '<h2>Sin turnos guardados</h2><p>Los turnos importados en el tablero se podrán consultar aquí.</p>';
  }
  const result = await request('/api/data');
  state.catalog = result.catalog;
  state.exportPath = result.export_path;
  state.turns = result.turns;
  if (state.turns.length) await loadTurn(state.turns[0].id);
  else render();
}

start().catch(error => status(error.message, true));
// Actualiza las sugerencias con el tiempo y evita interrumpir una edición en curso.
setInterval(async () => {
  if (!state.turn || dialog.open || state.busy || selection || document.hidden) return;
  const id = state.turn.id;
  try {
    const fresh = await request('/api/turn?id=' + encodeURIComponent(id));
    if (id === state.turn?.id && !dialog.open && !state.busy && !selection &&
      JSON.stringify(state.suggestions) !== JSON.stringify(fresh.suggestions)) {
      if (!board.contains(document.activeElement)) {
        state.suggestions = fresh.suggestions;
        renderBoard();
      }
    }
  } catch (error) {
    status(error.message, true);
  }
}, 30000);
