# Geotag Cascade GCV

Script en Python para **geolocalizar automáticamente fotos** usando:

- Google Cloud Vision (landmarks, Web Detection y OCR)
- Wikipedia + Nominatim (OpenStreetMap) para resolver nombres → coordenadas
- pHash para reutilizar localizaciones entre fotos parecidas
- Archivo de plan (`plan.json`) como **bias** principal (rango de fotos → lugar)
- Escritura de coordenadas en EXIF/XMP con `exiftool`
- Reindexado automático en Synology Photos mediante `touch`

> Pensado para procesar carpetas grandes de fotos de viajes, manteniendo la precisión gracias a un plan de rangos.

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

Si no está en el PATH, luego puedes indicar la ruta con `--exiftool-path`.

### 1.2. Dependencias Python

En tu entorno virtual (recomendado):

```bash
pip install exifread geopy tqdm requests google-cloud-vision
pip install pillow imagehash wikipedia charset-normalizer
pip install beautifulsoup4
```

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
     - A partir de `plan.json` (rango → lugar) si se pasó `--file`
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
     - se limita la distancia a un radio de **50 km** respecto al bias  
       (`DEFAULT_MAX_KM_BIAS`) para evitar saltos absurdos (p.ej. Tokio → Osaka).
4. Escribe las coordenadas en EXIF + XMP con `exiftool`.
5. Hace `touch` al fichero para que Synology Photos lo reindexe.
6. Genera siempre un log `result.csv` con el detalle de lo hecho.

---

## 4. Archivo de plan: `plan.json`

El plan es una lista de objetos con:

- `range`: `[inicio, fin]` de índices de foto (base 1)
- `hint`: una descripción de lugar que Nominatim puede resolver (p.ej. `"Tokyo, Japon"`)

Ejemplo:

```json
[
  { "range": [1, 111],  "hint": "Tokyo, Japon" },
  { "range": [112, 129], "hint": "Nikko, Japon" },
  { "range": [130, 186], "hint": "Fujiyoshida, Japon" },
  { "range": [187, 241], "hint": "Kyoto, Japon" },
  { "range": [242, 255], "hint": "Osaka, Japon" },
  { "range": [256, 265], "hint": "Hiroshima, Japon" },
  { "range": [266, 277], "hint": "Itsukushima, Japon" },
  { "range": [278, 295], "hint": "Nagano, Japon" },
  { "range": [296, 317], "hint": "Tokyo, Japon" }
]
```

Notas:

- Los índices son **globales, desde 1**, en el orden en el que el script procesa las fotos.
- El script:
  - Aplica directamente el hint si la foto cae dentro de un rango.
  - Si el índice no está en ningún rango:
    - Usa **siempre el último hint del JSON** como bias.

---

## 5. Parámetros de línea de comandos

Uso básico:

```bash
python geotag_cascade_gcv.py PATH [opciones]
```

Parámetros:

- `PATH`  
  Carpeta con fotos (no recursivo).

Opciones:

- `--file PLAN` / `--file plan.json`  
  Ruta al archivo de plan por rangos.  
  Si se pasa, es la fuente de bias principal.

- `--hint "Texto, Ciudad, País"`  
  Uno o varios hints separados por coma (se usa el primero) **solo si no hay plan**.
  Ejemplo: `--hint "Tokio, Japon, Kyoto, Japon"`

- `--dry-run`  
  No escribe EXIF (simulación).  
  Aun así genera `result.csv` para ver qué habría hecho.

- `--start-index N`  
  Índice global de foto (ordenada, **empezando en 1**) desde el que empezar a procesar.  
  Útil si ya procesaste las primeras N-1 fotos.

- `--end-index M`  
  Índice global de foto hasta el que procesar (inclusive).  
  Permite procesar solo un subrango.

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

### 6.1. Caso típico con `plan.json`

```bash
python geotag_cascade_gcv.py   "/volume1/homes/decompetynas/Photos/Variadas Miguel/2007_06_Japon/Ivan Japan Jun_07/"   --file "plan.json"   --verbose
```

- Procesa todas las fotos de la carpeta.
- Usa el `plan.json` para asignar bias por rangos.
- Lanza Google Cloud Vision + Wikipedia + Nominatim respetando radios de 50 km.
- Escribe EXIF y genera `result.csv`.

---

### 6.2. Reanudar a partir de una foto concreta

Supón que ya procesaste las 150 primeras fotos y quieres continuar desde la 151:

```bash
python geotag_cascade_gcv.py   "/volume1/homes/decompetynas/Photos/Variadas Miguel/2007_06_Japon/Ivan Japan Jun_07/"   --file "plan.json"   --start-index 151   --verbose
```

Las fotos 1–150 se marcarán como `skip_start_index` en el CSV y no se tocan.

---

### 6.3. Procesar solo un rango (p.ej. 100–200)

```bash
python geotag_cascade_gcv.py   "/volume1/homes/decompetynas/Photos/Variadas Miguel/2007_06_Japon/Ivan Japan Jun_07/"   --file "plan.json"   --start-index 100   --end-index 200   --verbose
```

Solo se procesan las fotos cuyo índice global está entre 100 y 200.

---

### 6.4. Modo simulación (sin tocar archivos)

```bash
python geotag_cascade_gcv.py   "/volume1/homes/decompetynas/Photos/Variadas Miguel/2007_06_Japon/Ivan Japan Jun_07/"   --file "plan.json"   --dry-run   --verbose
```

- No escribe nada en los EXIF.
- Aun así, genera un `result.csv` con lo que **habría** escrito.

---

### 6.5. Recalcular todo aunque ya tenga GPS

```bash
python geotag_cascade_gcv.py   "/volume1/homes/decompetynas/Photos/Variadas Miguel/2007_06_Japon/Ivan Japan Jun_07/"   --file "plan.json"   --force   --verbose
```

- Incluso las fotos con GPS previo se recalculan y se reescribe su posición.
- En el CSV verás entradas `force_overwrite_has_gps` para esas fotos.

---

## 7. Salida: `result.csv`

Siempre se genera un fichero `result.csv` en el directorio donde ejecutas el script.

Campos:

- `file` – ruta completa del fichero.
- `action` – acción realizada:
  - `write_gcv`, `write_web`, `write_ocr`, `write_phash`, `write_hint_seed_file`,
  - `write_last_known`, `skip_has_gps`, `force_overwrite_has_gps`,
  - `gcv_empty`, `gcv_error`, `skip_no_source`, etc.
- `lat`, `lon` – coordenadas escritas (si aplica).
- `source` – detalle de cómo se obtuvo:
  - p.ej. `detected:gcv:Tokyo Tower:0.87`, `derived_from_web:Tokio:wikipedia-es`,
  - `assigned_hint_seed_file:Kyoto, Japon`, etc.

Este CSV es muy útil para revisar casos raros, depurar o detectar fotos que el script no ha podido geolocalizar.

---

## 8. Notas y recomendaciones

- Ajusta `DEFAULT_MAX_KM_BIAS` en el código si quieres ser más o menos estricto:
  - 50 km va bien para ciudades grandes / áreas metropolitanas.
  - 20 km puede ser mejor para rutas más compactas.
- Si editas `plan.json`, recuerda que:
  - Los índices son base 1.
  - El script interpreta cualquier índice fuera de todos los rangos como “usa el último hint”.
- En caso de errores repetidos de la API de Vision:
  - Revisa tu cuota en Google Cloud.
  - Comprueba el `GOOGLE_APPLICATION_CREDENTIALS`.

Con esto deberías tener todo lo necesario para usar y mantener el script a largo plazo.
