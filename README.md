# Geotag Cascade GCV Multi ✅

Breve: script en Python para geolocalizar fotos automáticamente usando Google Cloud Vision (landmarks, web detection, OCR), Wikipedia/Nominatim y pHash. Soporta un plan por rangos (single) y procesamiento por lotes de carpetas (multi). Escribe coordenadas en EXIF/XMP con `exiftool` y genera un CSV con el detalle.

---

## Requisitos rápidos

- Python 3.9+ (recomendado 3.11)
- `exiftool` en PATH o indicar con `--exiftool-path`
- Instalar dependencias:

```bash
pip install exifread geopy tqdm requests google-cloud-vision pillow imagehash wikipedia charset-normalizer beautifulsoup4 lxml
```

Opcional (desarrollo): `pytest`, `ruff`, `black`.

---

## Comportamiento esencial

- Procesa fotos en orden por fecha (EXIF → mtime).
- Prioridad de geolocalización por foto:
  1. Landmark (Google Vision)
  2. Reuso por pHash
  3. Web Detection → etiquetas → Wikipedia/Nominatim
  4. OCR → texto → Wikipedia/Nominatim
  5. Fallback: `plan.json` (por rango) o `last_known`/`--hint`
- Si existe plan/hint, se aplica un radio máximo (por defecto **20 km**) para evitar saltos erróneos.
- Escribe GPS con `exiftool` (verificado tras escritura, con reintentos), hace `touch` y genera `result.csv` o `result_<carpeta>.csv` (multi).

---

## Planes y formatos

- Single: `plan.json` — lista de tags por índice. Soporta **dos formatos** compatibles:
  - `{"last": N, "hint": "Lugar"}` — indica que las fotos desde el inicio del bloque anterior + 1 (o 1 para el primer bloque) hasta `N` inclusive se asignan a ese hint.
  - `{"range": [start,end], "hint": "Lugar"}` — formato antiguo, aún soportado (interpretado como `[start,end]`).
  Si un índice no está en ningún bloque, se usa el **último hint conocido** como fallback (si existe).
- Multi: `plan_multi.json` — lista de `{"name","path","tags"}`. Si `path` está vacío se puede usar `--base-path`.

---

## Opciones importantes

- `--file PLAN` / `--multi-plan PLAN_MULTI` — detecta formato automáticamente
- `--base-path PATH`, `--hint`, `--dry-run`, `--start-index`, `--end-index`
- `--gcv-minconf`, `--gcv-timeout`, `--exiftool-path`, `--force`, `--verbose`
- `--path-map OLD:NEW`, `--apply-path-maps`, `--fail-on-missing`
- `--remove-loc-file FILE` — Ruta a un JSON (array de rutas de carpetas) con carpetas en las que eliminar la geolocalización. Si se pasa, el script mostrará las rutas resueltas y pedirá confirmación interactiva antes de proceder. Usa `--yes` para omitir la confirmación. Respeta `--dry-run` y `--exiftool-path`.
- Al usar `--remove-loc-file`, el script por defecto intentará eliminar las **mismas etiquetas GPS (EXIF + XMP)** que crea cuando añade localizaciones. Esto permite revertir localizaciones creadas previamente por este mismo script. Usa `--dry-run` para simular y `--yes` para omitir confirmación interactiva.
- `--yes` — Omitir confirmación interactiva (útil para ejecuciones no interactivas o en scripts automatizados).
- `--no-override-creds` — no sobrescribir `GOOGLE_APPLICATION_CREDENTIALS` desde `google_geo_tag.json`

---

## Ejemplos rápidos

- Single:
  `python geotag_cascade_gcv_multi.py /ruta/fotos --file plan.json --verbose`
- Multi:
  `python geotag_cascade_gcv_multi.py --file plan_multi.json --base-path /ruta/base --verbose`
- Dry-run:
  `python geotag_cascade_gcv_multi.py /ruta/fotos --file plan.json --dry-run`

---

## Eliminar geolocalización (remove_loc.json) 🔧

- Formato: el archivo debe ser un JSON que contenga **solo** un array de rutas (strings). Ejemplo:

```json
[
  "/ruta/a/carpeta1",
  "/otra/carpeta"
]
```

- Uso básico:
  - `python geotag_cascade_gcv_multi.py --remove-loc-file remove_loc.json`
  - `--dry-run` muestra qué se haría sin escribir EXIF.
  - `--yes` omite la confirmación interactiva y permite ejecución en entornos no interactivos.

- Comportamiento:
  - El script **resuelve** todas las rutas primero (expande `~`, `$VARS` y aplica `--path-map`/`DEFAULT_PATH_MAPS`).
  - Muestra un resumen con cada ruta resuelta y su estado (`EXISTS` / `MISSING`).
  - Pide confirmación interactiva: "¿Continuar y eliminar geolocalización en las carpetas listadas? [y/N]:". Si contestas no o presionas Enter, la operación se cancela sin tocar nada.
  - Si confirmas (o si pasas `--yes`), el script recorre las imágenes (no recursivo) y elimina campos GPS EXIF + XMP usando `exiftool` (se hace `touch` para reindexar). Si `exiftool` no está disponible y no usas `--dry-run`, el script falla con error.

- Ejemplos:

```bash
# Pregunta y luego ejecuta
python geotag_cascade_gcv_multi.py --remove-loc-file remove_loc.json

# Ejecutar sin confirmación (útil en scripts)
python geotag_cascade_gcv_multi.py --remove-loc-file remove_loc.json --yes

# Simular (no escribir)
python geotag_cascade_gcv_multi.py --remove-loc-file remove_loc.json --dry-run
```

- Tests: se añadieron pruebas básicas en `tests/test_remove_loc.py` que simulan aceptar y cancelar la confirmación.

---

## Salida

- `result.csv` (single) o `result_<carpeta>.csv` (multi)
- Campos: `file, action, lat, lon, source`

---
## Troubleshooting — Synology NAS

- exiftool no encontrado: instala con `opkg install exiftool` o indica la ruta con `--exiftool-path /opt/bin/exiftool` o ajusta `exiftool.path` en `config.json`.
- Paths montados diferentes: usa `--path-map OLD:NEW` (ej. `/var/services/homes:/volume1/homes`) y `--apply-path-maps` para actualizar el JSON (se hace backup automáticamente).
- Permisos: asegúrate de que el usuario que ejecuta el script puede leer las fotos y ejecutar `exiftool`.
- Credenciales: coloca `google_geo_tag.json` en el directorio del script o exporta `GOOGLE_APPLICATION_CREDENTIALS`. Si quieres preservar la variable existente, usa `--no-override-creds`.
- Synology Photos no reindexa: el script hace `touch`; si no funciona, prueba a forzar reindex o reiniciar el servicio de Photos.
- Vision API / cuota: revisa el proyecto en Google Cloud (roles, cuotas) y valida que la cuenta de servicio tenga permisos de Vision.
- Nominatim / rate limit: respeta timeouts y evita consultas masivas; cachea resultados si haces muchas resoluciones.

Consejos de diagnóstico:
- Ejecuta `--dry-run --verbose` para ver las decisiones sin escribir EXIF.
- Revisa `result*.csv` para ver por foto la acción tomada.

---
## Notas

- `DEFAULT_MAX_KM_BIAS` (20 km) evita saltos geográficos erróneos.
- Si pones `google_geo_tag.json` junto al script, se intentará usarlo automáticamente (se valida como JSON). Añadí `.gitignore` para ese archivo y `.venv`, `__pycache__`.
- Tests: `python tests/run_simple_tests.py` (sin deps externos) — usar `pytest` para pruebas completas.

**Fallback de hints no resueltos:**
- Cuando un `hint` no puede resolverse con Nominatim/Wikipedia, el script intentará **geocodificar solo la parte 'país'** (última porción tras la última coma del `hint`). Por ejemplo, `"Mostoles, Spain"` → intentará `"Spain"`.
- Si la geocodificación por país tiene éxito, se usará esa coordenada y se registrará una entrada `hint_fallback_used:<original>-><country>` en los errores/logs.
- Si ambos intentos fallan, el script no dispondrá de coordenadas numéricas para ese bloque; en ese caso se guarda un fallback textual (por ejemplo, `"Spain"`) para que el pipeline pueda usar tokens (p. ej. búsqueda de Wikipedia o match por etiquetas) sin imponer una restricción de bias.
- Logs/errores que puedes ver: `hint_fallback_used:<name>-><country>`, `hint_unresolved:<name>`, `hint_geocode_error:<name>:<error>`.

Si quieres, convierto esto a una versión aún más corta (1 página) o añado un apartado de troubleshooting para Synology NAS.

## 4. Formatos de archivo de plan

El script **detecta automáticamente** el formato del JSON. Soporta dos tipos:

### 4.1. Formato Single (`plan.json`) - Una carpeta

Formato tradicional para procesar una sola carpeta. Soporta ahora dos formas:

- `{"last": N, "hint": "Lugar"}` — cada entrada indica el **último índice** (inclusive) para ese bloque; los bloques se ordenan por `last` ascendente y el primero comienza en 1. Por ejemplo, `{"last": 14, "hint": "Dublin, Ireland"}` significa que las fotos 1..14 usarán ese hint.
- `{"range": [inicio, fin], "hint": "Lugar"}` — formato antiguo, todavía válido.

Ejemplos:

```json
[
  { "last": 111,  "hint": "Tokyo, Japan" },
  { "last": 129,  "hint": "Nikko, Japan" },
  { "last": 186,  "hint": "Fujiyoshida, Japan" },
  { "last": 241,  "hint": "Kyoto, Japan" }
]
```

Notas:

- Los índices son **globales, desde 1**, en el orden en el que el script procesa las fotos.
- El script:
  - Asigna a cada foto el hint del bloque correspondiente según `last`/`range`.
  - Si un índice no está en ningún bloque, usa **el último hint conocido** (si existe) como fallback.
  - Si un hint no puede resolverse geográficamente, el script intentará un **fallback por país** (ver sección siguiente).

### 4.2. Formato Multi (`plan_multi.json`) - Múltiples carpetas

Formato nuevo para procesar múltiples carpetas en batch. Es una lista de objetos con:

- `name`: nombre de la carpeta (se usa para construir la ruta si `path` está vacío)
- `path`: ruta completa a la carpeta (si está vacío, se construye desde `--base-path` + `name`)
- `tags`: array de objetos con `range` y `hint` (igual que el formato single, pero por carpeta)

Ejemplo:

```json
[
  {
    "name": "Japon 2019",
    "path": "",
    "tags": [
      { "range": [1, 50], "hint": "Tokyo, Japon" },
      { "range": [51, 100], "hint": "Kyoto, Japon" }
    ]
  },
  {
    "name": "Corea 2020",
    "path": "/ruta/completa/Corea 2020",
    "tags": [
      { "range": [1, 30], "hint": "Seoul, South Korea" }
    ]
  }
]
```

Notas:

- Si `path` está vacío, el script construye la ruta como `--base-path` + `name`.
- Cada carpeta se procesa independientemente con sus propios tags.
- Cada carpeta genera su propio CSV: `result_[nombre_carpeta].csv`.

---

## 5. Parámetros de línea de comandos

### 5.1. Uso básico (una carpeta)

```bash
python geotag_cascade_gcv_multi.py PATH [opciones]
```

### 5.2. Uso multi-carpeta

```bash
python geotag_cascade_gcv_multi.py --file plan_multi.json --base-path /ruta/base [opciones]
# o explícitamente:
python geotag_cascade_gcv_multi.py --multi-plan plan_multi.json --base-path /ruta/base [opciones]
```

### 5.3. Parámetros principales

- `PATH` (opcional si se usa `--file` con formato multi)  
  Carpeta con fotos (no recursivo). Requerido solo en modo single.

### 5.4. Opciones

- `--file PLAN` / `--file plan.json`  
  Ruta al archivo JSON de plan. **Detecta automáticamente** si es formato single o multi:
  - **Single**: procesa la carpeta especificada en `PATH` con ese plan
  - **Multi**: procesa todas las carpetas listadas en el JSON (ignora `PATH`)
  
  Ejemplos:
  ```bash
  # Formato single
  python geotag_cascade_gcv_multi.py /ruta/carpeta --file plan.json
  
  # Formato multi (detecta automáticamente)
  python geotag_cascade_gcv_multi.py --file plan_multi.json --base-path /ruta/base
  ```

- `--multi-plan PLAN_MULTI`  
  Ruta al archivo JSON multi-carpeta (formato multi).  
  Equivalente a `--file` cuando el JSON es formato multi.  
  Útil para ser explícito sobre el tipo de procesamiento.

- `--base-path RUTA`  
  Ruta base para construir paths cuando `path` está vacío en el JSON multi.  
  Solo necesario en modo multi-plan.

- `--hint "Texto, Ciudad, País"`  
  Uno o varios hints separados por coma (se usa el primero) **solo si no hay plan**.  
  Ejemplo: `--hint "Tokio, Japon, Kyoto, Japon"`

- `--dry-run`  
  No escribe EXIF (simulación).  
  Aun así genera `result.csv` para ver qué habría hecho.

- `--start-index N`  
  Índice global de foto (ordenada, **empezando en 1**) desde el que empezar a procesar.  
  Útil si ya procesaste las primeras N-1 fotos.  
  En modo multi, se aplica a cada carpeta independientemente.

- `--end-index M`  
  Índice global de foto hasta el que procesar (inclusive).  
  Permite procesar solo un subrango.  
  En modo multi, se aplica a cada carpeta independientemente.

- `--gcv-minconf VAL`  
  Confianza mínima para aceptar un landmark de Google Cloud Vision.  
  Por defecto: `0.60`.

- `--gcv-timeout SEG`  
  Timeout en segundos para cada llamada a Vision.  
  Por defecto: `20.0`.

- `--verbose`  
  Muestra por consola la acción tomada para cada foto.

- `--exiftool-path RUTA`  
  Ruta al binario de `exiftool` (p. ej. `/opt/bin/exiftool` en NAS).

- `--force`  
  Fuerza escritura de localización **aunque la foto ya tenga GPS**.  
  En el log se marcarán esas fotos como `force_overwrite_has_gps`.

- `--path-map OLD:NEW`  
  Mapea prefijos de ruta cuando las rutas listadas en `plan_multi.json` no coinciden con el montaje actual (útil en NAS con distintos prefijos como `/var/services/homes` vs `/volume1/homes`). Repetible. Ejemplo:
  ```bash
  --path-map /var/services/homes:/volume1/homes
  ```

- `--apply-path-maps`  
  Aplica los `--path-map` (y los mappings por defecto) directamente al archivo `plan_multi.json` (se hace un backup `.bak` antes de sobrescribir). Útil para arreglar el archivo de forma permanente.

- `--fail-on-missing`  
  Si hay carpetas listadas en `plan_multi.json` que no existen tras aplicar los mappings, salir con error antes de procesar (por defecto se ignoran y se saltan).

- `--no-override-creds`  
  Si se pasa, el script **no** sobrescribirá `GOOGLE_APPLICATION_CREDENTIALS` desde `google_geo_tag.json` (si existe), permitiendo que la variable de entorno preexistente tenga prioridad.

---

## 6. Ejemplos de uso

### 6.1. Caso típico con `plan.json` (formato single)

```bash
python geotag_cascade_gcv_multi.py \
  "/volume1/homes/user/Photos/Viajes/Japon 2019/" \
  --file "plan.json" \
  --verbose
```

- Procesa todas las fotos de la carpeta.
- Usa el `plan.json` para asignar bias por rangos.
- Lanza Google Cloud Vision + Wikipedia + Nominatim respetando radios de 20 km.
- Escribe EXIF y genera `result.csv`.

### 6.2. Procesar múltiples carpetas con `plan_multi.json`

```bash
python geotag_cascade_gcv_multi.py \
  --file "plan_multi.json" \
  --base-path "/volume1/homes/user/Photos/Viajes/" \
  --verbose
```

- Detecta automáticamente que es formato multi.
- Procesa todas las carpetas listadas en el JSON.
- Cada carpeta usa sus propios tags (rangos y hints).
- Genera un CSV por carpeta: `result_Japon_2019.csv`, `result_Corea_2020.csv`, etc.

### 6.3. Usar `--multi-plan` explícitamente

```bash
python geotag_cascade_gcv_multi.py \
  --multi-plan "plan_multi.json" \
  --base-path "/volume1/homes/user/Photos/Viajes/" \
  --verbose
```

Equivalente al ejemplo anterior, pero siendo explícito sobre el modo multi.

### 6.4. Reanudar a partir de una foto concreta (modo single)

Supón que ya procesaste las 150 primeras fotos y quieres continuar desde la 151:

```bash
python geotag_cascade_gcv_multi.py \
  "/volume1/homes/user/Photos/Viajes/Japon 2019/" \
  --file "plan.json" \
  --start-index 151 \
  --verbose
```

Las fotos 1–150 se marcarán como `skip_start_index` en el CSV y no se tocan.

### 6.5. Procesar solo un rango (p.ej. 100–200)

```bash
python geotag_cascade_gcv_multi.py \
  "/volume1/homes/user/Photos/Viajes/Japon 2019/" \
  --file "plan.json" \
  --start-index 100 \
  --end-index 200 \
  --verbose
```

Solo se procesan las fotos cuyo índice global está entre 100 y 200.

### 6.6. Modo simulación (sin tocar archivos)

```bash
python geotag_cascade_gcv_multi.py \
  "/volume1/homes/user/Photos/Viajes/Japon 2019/" \
  --file "plan.json" \
  --dry-run \
  --verbose
```

- No escribe nada en los EXIF.
- Aun así, genera un `result.csv` con lo que **habría** escrito.

### 6.7. Recalcular todo aunque ya tenga GPS

```bash
python geotag_cascade_gcv_multi.py \
  "/volume1/homes/user/Photos/Viajes/Japon 2019/" \
  --file "plan.json" \
  --force \
  --verbose
```

- Incluso las fotos con GPS previo se recalculan y se reescribe su posición.
- En el CSV verás entradas `force_overwrite_has_gps` para esas fotos.

### 6.8. Procesar múltiples carpetas con exiftool en ruta personalizada

```bash
python geotag_cascade_gcv_multi.py \
  --file "plan_multi.json" \
  --base-path "/volume1/homes/user/Photos/Viajes/" \
  --exiftool-path "/opt/bin/exiftool" \
  --verbose
```

python geotag_cascade_gcv_multi.py \
  --file "plan_multi.json" \
  --base-path "/var/services/homes/decompetynas/Photos/" \
  --verbose

Útil en NAS donde `exiftool` puede estar en una ruta no estándar.

---

## Notas adicionales

- Mappings por defecto: el script incluye un mapping por defecto útil en Synology NAS: `/var/services/homes -> /volume1/homes`.
- Logging: el script ahora usa el módulo `logging`. Usa `--verbose` para ver mensajes informativos por carpeta/foto.
- Credenciales: si colocas `google_geo_tag.json` junto al script, se intentará usarlo automáticamente (puedes desactivar ese comportamiento con `--no-override-creds`).
- Tests: he añadido pruebas simples en `tests/test_multi_plan.py` y un runner `tests/run_simple_tests.py`. Ejecuta `pytest` o `python tests/run_simple_tests.py` para verificar `resolve_folder_path` y `load_multi_plan`.

Si quieres que actualice este README con ejemplos de `plan_multi.json` reales o añadir una sección de troubleshooting para NAS, dime y la incluyo.

## 7. Salida: `result.csv`

Siempre se genera un fichero CSV con el detalle de lo procesado:

- **Modo single**: `result.csv` en el directorio donde ejecutas el script.
- **Modo multi**: `result_[nombre_carpeta].csv` por cada carpeta procesada (en el mismo directorio de ejecución).

### 7.1. Campos del CSV

- `file` – ruta completa del fichero.
- `action` – acción realizada:
  - `write_gcv`, `write_web`, `write_ocr`, `write_phash`, `write_hint_seed_file`,
  - `write_last_known`, `skip_has_gps`, `force_overwrite_has_gps`,
  - `gcv_empty`, `gcv_error`, `skip_no_source`, `skip_start_index`, `skip_end_index`, etc.
- `lat`, `lon` – coordenadas escritas (si aplica).
- `source` – detalle de cómo se obtuvo:
  - p.ej. `detected:gcv:Tokyo Tower:0.87`, `derived_from_web:Tokio:wikipedia-es`,
  - `assigned_hint_seed_file:Kyoto, Japon`, etc.

Este CSV es muy útil para revisar casos raros, depurar o detectar fotos que el script no ha podido geolocalizar.

---

## 8. Detección automática de formato

El script detecta automáticamente el tipo de JSON:

- **Formato Single**: objetos con `range` y `hint` → procesa una carpeta
- **Formato Multi**: objetos con `name`, `path` y `tags` → procesa múltiples carpetas

No necesitas especificar el tipo; el script lo detecta automáticamente cuando usas `--file`.

---

## 9. Notas y recomendaciones

- Ajusta `DEFAULT_MAX_KM_BIAS` en el código si quieres ser más o menos estricto:
  - 20 km va bien para ciudades grandes / áreas metropolitanas.
  - 50 km puede ser mejor para rutas más dispersas.
- Si editas `plan.json` o `plan_multi.json`, recuerda que:
  - Los índices son base 1.
  - El script interpreta cualquier índice fuera de todos los rangos como "usa el último hint".
- En modo multi-plan:
  - Cada carpeta se procesa independientemente.
  - Los índices en `tags` son relativos a cada carpeta (empiezan en 1 para cada una).
  - Si una carpeta no existe, se muestra un warning y se continúa con la siguiente.
- En caso de errores repetidos de la API de Vision:
  - Revisa tu cuota en Google Cloud.
  - Comprueba el `GOOGLE_APPLICATION_CREDENTIALS`.
- El script no es recursivo: solo procesa archivos en el directorio especificado, no en subdirectorios.

---

## 10. Resumen de modos de uso

| Modo | Comando | JSON | Salida CSV |
|------|---------|------|------------|
| **Single** | `python script.py /ruta/carpeta --file plan.json` | Formato single | `result.csv` |
| **Multi** | `python script.py --file plan_multi.json --base-path /ruta` | Formato multi | `result_[carpeta].csv` (uno por carpeta) |
| **Multi explícito** | `python script.py --multi-plan plan_multi.json --base-path /ruta` | Formato multi | `result_[carpeta].csv` (uno por carpeta) |

Con esto deberías tener todo lo necesario para usar y mantener el script a largo plazo, tanto para procesar carpetas individuales como para procesar múltiples carpetas en batch.
