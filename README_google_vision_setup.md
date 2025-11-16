# Guía rápida: Configurar Google Cloud Vision para el script de geotagging

Este documento explica **cómo crear un proyecto en Google Cloud**, activar Vision API, generar una **clave de servicio**, descargar el JSON y configurarlo en tu NAS/Linux para usarlo con tu script de geolocalización.

---

# 1. Crear cuenta en Google Cloud (si no tienes)
1. Ve a https://cloud.google.com/
2. Inicia sesión con tu cuenta Google.
3. Acepta los términos iniciales y configura un proyecto por defecto.

---

# 2. Crear un **nuevo proyecto**
1. En la barra superior, clic en **Select Project**.
2. Clic en **New Project**.
3. Nombre sugerido: `Geotagging` o `Photo-GCV`.
4. Crear.

---

# 3. Activar la API **Cloud Vision**
1. Con el proyecto seleccionado, ve a:
   - https://console.cloud.google.com/apis/library/vision.googleapis.com
2. Haz clic en **Enable** (Activar).

---

# 4. Crear una **Service Account** (cuenta de servicio)
1. Ve a:
   - https://console.cloud.google.com/iam-admin/serviceaccounts
2. Clic en **Create Service Account**.
3. Nombre: `geotagging-sa`
4. Permisos:
   - Seleccionar rol → **Project** → **Editor**
   - (O el rol más limitado: *Cloud Vision API User*)
5. Crear.

---

# 5. Crear y descargar el **archivo de credenciales JSON**
1. Dentro de tu Service Account recién creada:
   - Clic en ella → pestaña **Keys**
2. Clic en **Add key** → **Create new key**
3. Tipo: **JSON**
4. Descargar.

Esto generará un archivo parecido a:

```
my-geotagging-project-1234567890abcd.json
```

**Guárdalo en un lugar seguro.**

---

# 6. Subir el JSON a tu NAS o máquina Linux
Por ejemplo, en Synology:

```
/volume1/homes/<usuario>/gcv_credentials.json
```

---

# 7. Exportar la variable de entorno
El SDK de Vision usa:

```
GOOGLE_APPLICATION_CREDENTIALS
```

Ejemplo:

```bash
export GOOGLE_APPLICATION_CREDENTIALS="/volume1/homes/decompetynas/gcv_credentials.json"
```

---

# 8. Verificar con un test rápido

```bash
python3 - << 'EOF'
from google.cloud import vision
client = vision.ImageAnnotatorClient()
print("Vision API OK")
EOF
```

---

# 9. Ver consumo y cuotas

https://console.cloud.google.com/apis/api/vision.googleapis.com/metrics

---

# 10. Ejemplo de uso completo

```bash
export GOOGLE_APPLICATION_CREDENTIALS="/volume1/homes/decompetynas/gcv_credentials.json"

python geotag_cascade_gcv.py   "/volume1/homes/decompetynas/Photos/2007_Japon/"   --file "plan.json"   --verbose
```

---

# 11. Consejos

- No compartas el JSON.
- No lo subas a GitHub.
- Elimínalo desde IAM si lo pierdes.

