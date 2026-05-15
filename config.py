# ─────────────────────────────────────────────
# Configuración centralizada (AOI, fechas, umbrales)
# ─────────────────────────────────────────────

import ee
from dotenv import load_dotenv
import os

load_dotenv()

def initialize_gee():
    """
    Autentica e inicializa la conexión con Google Earth Engine.
    Usa el Project ID de tu .env para no hardcodear credenciales.
    """
    project_id = os.getenv("GEE_PROJECT_ID")
    if not project_id:
        raise ValueError("GEE_PROJECT_ID no encontrado en .env")
    
    ee.Initialize(project=project_id)
    print(f"GEE inicializado correctamente con proyecto: {project_id}")

# --- Inicializar GEE ---
initialize_gee()

# --- Área de Interés: Catalunya ---
# Bounding box conservador que cubre todo el territorio.
# lon_min, lat_min, lon_max, lat_max
AOI = ee.Geometry.Rectangle([0.15, 40.51, 3.33, 42.86])

# --- Periodos de comparación ---
# Usamos julio-agosto: máximo estrés hídrico estival, menos nubes.
# Evitamos septiembre porque las primeras lluvias pueden enmascarar el efecto sequía.
PERIOD_BASELINE = {
    "name": "verano_2021",
    "start": "2021-07-01",
    "end":   "2021-08-31",
}

PERIOD_COMPARE = {
    "name": "verano_2024",
    "start": "2024-07-01",
    "end":   "2024-08-31",
}

# Máximo % de nubes permitido por imagen antes de descartarla.
# Catalunya en verano tiene poco nubosidad — 15% es razonable.
# Si GEE devuelve 0 imágenes, súbe el valor a 25.
MAX_CLOUD_COVER = 15

# Resolución de exportación en metros/pixel.
EXPORT_SCALE = 100