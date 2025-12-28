# Geotag Cascade GCV Multi

Script en Python para **geolocalizar automáticamente fotos** usando:

- Google Cloud Vision (landmarks, Web Detection y OCR)
- Wikipedia + Nominatim (OpenStreetMap) para resolver nombres → coordenadas
- pHash para reutilizar localizaciones entre fotos parecidas
- Archivo de plan (`plan.json` o `plan_multi.json`) como **bias** principal (rango de fotos → lugar)
- Escritura de coordenadas en EXIF/XMP con `exiftool`
- Reindexado automático en Synology Photos mediante `touch`
- **Soporte para procesar múltiples carpetas** desde un único archivo JSON

> Pensado para procesar carpetas grandes de fotos de viajes, manteniendo la precisión gracias a un plan de rangos. Ahora también permite procesar múltiples carpetas en batch.

---

## 1. Requisitos

### 1.1. Dependencias del sistema

- Python 3.9+ (idealmente 3.11 en tu NAS/PC)
- `exiftool` instalado y accesible en el `PATH`  
  En Synology (entorno Entware/Optware) suele ser algo como:

```bash
opkg install exiftool
# y normalmente queda en /opt/bin/exiftool
```

Si no está en el PATH, puedes configurarlo en `config.json` o indicar la ruta con `--exiftool-path`.

### 1.2. Dependencias Python

En tu entorno virtual (recomendado):

```bash
pip install exifread geopy tqdm requests google-cloud-vision
pip install pillow imagehash wikipedia charset-normalizer
pip install beautifulsoup4
```

### 1.3. Archivo de configuración (opcional)

El script puede usar un archivo `config.json` para configurar valores por defecto. Si no existe, se usan valores predefinidos.

Copia el archivo de ejemplo y personalízalo:

```bash
cp config.json.example config.json
```

Estructura del `config.json`:

```json
{
  "gcv": {
    "minconf": 0.60,
    "timeout": 20.0
  },
  "geocoding": {
    "timeout": 15.0,
    "max_km_bias": 20.0,
    "max_km_if_bias": 50.0
  },
  "exiftool": {
    "path": "exiftool"
  },
  "output": {
    "csv_prefix": "result"
  }
}
```

**Parámetros configurables:**

- `gcv.minconf`: Confianza mínima para aceptar landmarks de Google Cloud Vision (default: 0.60)
- `gcv.timeout`: Timeout en segundos para llamadas a Vision API (default: 20.0)
- `geocoding.timeout`: Timeout para geocodificación con Nominatim (default: 15.0)
- `geocoding.max_km_bias`: Radio máximo en km alrededor del hint del plan (default: 20.0)
- `geocoding.max_km_if_bias`: Radio máximo para validación en resolución de nombres (default: 50.0)
- `exiftool.path`: Ruta al binario exiftool (default: "exiftool")
- `output.csv_prefix`: Prefijo para archivos CSV generados (default: "result")

**Nota:** Los valores pasados por línea de comandos tienen prioridad sobre `config.json`.

---

## 2. Autenticación Google Cloud Vision

El script usa el **SDK oficial de Google Cloud Vision** con transporte REST.

1. Crea un proyecto en Google Cloud.
2. Activa la API **Cloud Vision**.
3. Crea una **cuenta de servicio** y descarga el JSON de credenciales.
4. Exporta la ruta al JSON:

```bash
export GOOGLE_APPLICATION_CREDENTIALS="/ruta/a/tu/credenciales.json"
```

En el NAS, esto lo puedes poner en el `.profile` o en el script que lance el entorno.

---

## 3. Estructura general

El script:

1. Lista todos los ficheros de imagen en una carpeta (no recursivo), extensiones:
   - `.jpg`, `.jpeg`, `.heic`, `.heif`, `.tif`, `.tiff`, `.png`,
   - `.dng`, `.nef`, `.cr2`, `.arw`, `.rw2`, `.orf`, `.raf`, `.srw`
2. Ordena las fotos por fecha de captura:
   - `EXIF DateTimeOriginal` → `Image DateTime` → `mtime`
3. Para cada foto (a partir de `start_index`, opcionalmente hasta `end_index`):
   - Obtiene un **bias** de localización:
     - A partir de `plan.json` (rango → lugar) si se pasó `--file` (formato single)
     - O desde `plan_multi.json` (múltiples carpetas) si se usa `--file` o `--multi-plan` (formato multi)
     - O del primer `--hint` global si no hay plan
   - Intenta localizar usando el siguiente pipeline:
     1. Google Cloud Vision **Landmarks**
     2. Reuso de pHash (si otra foto similar ya fue resuelta)
     3. Web Detection (GCV) → etiquetas → Wikipedia/Nominatim
     4. OCR (GCV) → texto → Wikipedia/Nominatim
     5. Si nada anterior funciona:
        - Usa directamente las coordenadas del hint del `plan.json` para ese índice
        - O, si no hay plan, `last_known` / `--hint`
   - En todos los métodos, si hay plan/hint:
     - se limita la distancia a un radio de **20 km** respecto al bias  
       (`DEFAULT_MAX_KM_BIAS`) para evitar saltos absurdos (p.ej. Tokio → Osaka).
4. Escribe las coordenadas en EXIF + XMP con `exiftool`.
5. Hace `touch` al fichero para que Synology Photos lo reindexe.
6. Genera siempre un log `result.csv` (o `result_[carpeta].csv` en modo multi) con el detalle de lo hecho.

---

## 4. Formatos de archivo de plan

El script **detecta automáticamente** el formato del JSON. Soporta dos tipos:

### 4.1. Formato Single (`plan.json`) - Una carpeta

Formato tradicional para procesar una sola carpeta. Es una lista de objetos con:

- `range`: `[inicio, fin]` de índices de foto (base 1)
- `hint`: una descripción de lugar que Nominatim puede resolver (p.ej. `"Tokyo, Japon"`)

Ejemplo:

```json
[
  { "range": [1, 111],  "hint": "Tokyo, Japon" },
  { "range": [112, 129], "hint": "Nikko, Japon" },
  { "range": [130, 186], "hint": "Fujiyoshida, Japon" },
  { "range": [187, 241], "hint": "Kyoto, Japon" }
]
```

Notas:

- Los índices son **globales, desde 1**, en el orden en el que el script procesa las fotos.
- El script:
  - Aplica directamente el hint si la foto cae dentro de un rango.
  - Si el índice no está en ningún rango:
    - Usa **siempre el último hint del JSON** como bias.

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

Útil en NAS donde `exiftool` puede estar en una ruta no estándar.

---

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
