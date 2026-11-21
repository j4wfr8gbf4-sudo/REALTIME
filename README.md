# REALTIME · Seguimiento de Turnos

Aplicación web local para importar el Plan de un turno desde la hoja `PROG` de un POD, registrar el Real y las causas de no cumplimiento (CNC) por frente, y actualizar un histórico en Excel. Los datos se guardan en SQLite en el equipo donde se ejecuta la aplicación.

## Ejecutar desde el repositorio

Usa Python 3.12 o 3.13. Ejecuta los comandos desde la raíz del repositorio.

**Windows (PowerShell)**

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

**Linux o macOS**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py
```

Abre `http://127.0.0.1:8765` si el navegador no se inicia automáticamente. Detén el servidor con Ctrl+C. Si quieres usar otro puerto, agrega `--puerto 8766`. Para arrancar sin abrir el navegador, agrega `--sin-navegador`. El servidor escucha en la interfaz local.

El ZIP original incluye un Python portátil y sus dependencias para Windows x64. Para usarlo sin instalar Python, extrae el ZIP completo y ejecuta `Iniciar_Windows.bat`. El lanzador busca `runtime\python.exe` en la misma carpeta. En el repositorio las dependencias se instalan con `requirements.txt`.

Si vienes de una instalación anterior, cierra primero la aplicación y copia su carpeta `datos` a la raíz de esta instalación, o apunta a ella con `--datos`.

## Uso durante el turno

1. Abre **Importar POD** y selecciona el Excel. Revisa los frentes, confirma la fecha operativa y carga el Plan. Si seleccionas un turno que ya existe, la aplicación pide confirmar el reemplazo del Plan.
2. En **Plan** o **Real**, selecciona un bloque o arrastra desde una celda vacía para marcar un rango de 15 minutos. Usa **Mayús + clic** para extender la selección. Escribe directamente para editar la sigla. Pulsa **Tab** para recorrer **Actividad → Cuadrilla → Equipo**, o **Mayús + Tab** para retroceder. Separa varios equipos o cuadrillas con comas. Pulsa **Enter** para guardar los campos modificados o **Esc** para descartarlos. Haz doble clic en un bloque para abrir el formulario completo. Para un solapamiento intencional, marca **Permitir actividad simultánea** en el formulario de Real.
3. En la línea **CNC**, los signos de interrogación indican diferencias pendientes de revisar. Abre el bloque para completar el Real, descartar la sugerencia o confirmar una CNC. Agrega los códigos y sus dos clasificaciones en **Catálogo CNC**. Las CNC ya registradas conservan la clasificación con la que se guardaron.
4. Registra el estado real del frente al cierre. Abre **Exportar a Excel** y confirma el destino de la base histórica. Si ya existe una base generada por la aplicación en esa ruta, se actualiza el turno seleccionado. Si Excel impide escribir, cierra el libro y reintenta.

### Mover, copiar y ajustar actividades

Arrastra un bloque de **Plan** a otra fila **Plan**, o de **Real** a otra fila **Real**, para reubicarlo. Arrástralo de **Plan** a **Real** para copiar la sigla, la duración, los equipos y la cuadrilla. El bloque original se conserva en el Plan. Una vista previa muestra el destino antes de soltar. El guardado valida los límites del turno y los solapamientos. Volver a importar un POD para el mismo turno con **Reemplazar Plan** sustituye también las ediciones manuales del Plan.

Para cambiar la duración, mantén **Ctrl** y arrastra el borde izquierdo o derecho del bloque. El otro borde queda fijo. El ajuste se hace en tramos de 15 minutos.

### Consultar turnos anteriores

Abre **Historial** para consultar los turnos guardados en otra pestaña. El selector muestra la fecha y el turno. La vista abre en **Solo lectura**. Selecciona **Activar edición** para modificar el turno que estás viendo. Para bloquear la edición, pulsa **Volver a solo lectura** o selecciona otro turno.

## Datos y respaldo

La carpeta `datos` contiene la base `turnos.sqlite3` y es el destino predeterminado de `Seguimiento_Turnos.xlsx`. El parámetro `--datos` permite elegir otra carpeta. Los cambios confirmados se guardan de inmediato. Mientras la aplicación esté abierta, cada 15 minutos se guarda una copia de la base en `datos/respaldos`. La aplicación conserva siete días de copias automáticas. El archivo de Excel se genera desde **Exportar a Excel**.

Para recuperar una copia, detén la aplicación, conserva el `turnos.sqlite3` actual en otra carpeta y copia el respaldo elegido a `datos` con el nombre `turnos.sqlite3`. Vuelve a iniciar la aplicación.

## Cómo funciona el código

| Archivo | Responsabilidad |
| --- | --- |
| `app.py` | Inicia el servidor HTTP local. Procesa la API y coordina las operaciones de importación, edición, catálogo y exportación. |
| `domain.py` | Identifica los turnos, valida sus intervalos, concilia los frentes al reimportar y calcula las sugerencias de CNC. |
| `excel.py` | Lee la hoja `PROG` con openpyxl y escribe el histórico en Excel. |
| `store.py` | Guarda los turnos, el catálogo y las opciones en SQLite. Gestiona transacciones y respaldos. |
| `static/index.html`, `static/app.css` y `static/app.js` | Definen la interfaz, dibujan el tablero y conectan los formularios y gestos con la API. |
| `test_app.py` | Comprueba las reglas y el flujo HTTP con una base temporal y POD de prueba. |

1. **Importación.** El navegador envía el Excel a `/api/preview`. El servidor valida la hoja `PROG`, lee sus 24 columnas de media hora y agrupa celdas consecutivas que tengan la misma sigla. Asocia a cada frente sus equipos y cuadrillas programados. Al confirmar la fecha, `/api/import` guarda el turno. Si se reemplaza el Plan, se conserva el Real, las CNC y el cierre registrado para cada frente.
2. **Horario y edición.** Un turno se identifica por fecha operativa y tipo. El turno de Día va de 08:00 a 20:00 y el de Noche, de 20:00 a 08:00 del día siguiente. El código usa minutos desde la medianoche de la fecha operativa. Por ejemplo, 01:00 del día siguiente corresponde al minuto 1500. El tablero usa intervalos de 15 minutos y `/api/front` valida y guarda los cambios en una transacción.
3. **Revisión.** Para cada intervalo terminado, el servidor compara la sigla planificada con el Real. Si falta la sigla esperada, el tablero muestra una sugerencia, salvo que ya exista una CNC en ese tramo o se haya descartado esa diferencia. Confirmar una CNC guarda el código y sus dos clasificaciones de catálogo como parte del registro del turno.
4. **Historial y exportación.** El historial lee los turnos guardados en SQLite. Al exportar, se actualizan las filas del turno seleccionado en cuatro hojas de Excel. Se escriben los frentes, las actividades, las asignaciones y las CNC. El código abre y verifica el libro temporal antes de sustituir el archivo de destino.

## Verificación

Ejecuta las pruebas con el Python del entorno virtual.

- Windows: `.\.venv\Scripts\python.exe -B test_app.py -v`
- Linux o macOS: `.venv/bin/python -B test_app.py -v`

Las pruebas cubren importación diurna y nocturna, persistencia, reemplazo del Plan, edición de actividades, CNC, respaldos, exportación y API HTTP. Para comprobar también un POD nocturno del formato que espera la prueba, agrega `--pod "ruta/al/POD.xlsm"` al comando.
