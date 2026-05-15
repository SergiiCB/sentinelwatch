# ─────────────────────────────────────────────
# Script de diagnóstico de conexión a GEE
# ─────────────────────────────────────────────

import os
import ee
from dotenv import load_dotenv

# Esto busca el archivo .env y lee tu ID
load_dotenv()
project_id = os.getenv('GEE_PROJECT_ID')

try:
    # Intentamos conectar con Google usando tu ID
    ee.Initialize(project=project_id)
    
    # Pedimos una pequeña prueba a los servidores de Google
    print("\n" + "="*30)
    print("¡CONEXIÓN EXITOSA!")
    print(f"ID del Proyecto: {project_id}")
    print("🛰️ Estado: Listo.")
    print("="*30)

except Exception as e:
    print("\n" + "!"*30)
    print(f"❌ ERROR: {e}")
    print("Revisa si habilitaste la API en la web de Google Cloud.")
    print("!"*30)