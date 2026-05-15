# ─────────────────────────────────────────────
# Hito 3: Vectorización e ingesta en PostGIS
# ─────────────────────────────────────────────

import numpy as np
import rasterio
import rasterio.features
import geopandas as gpd
import psycopg2
from psycopg2.extras import execute_values
from shapely.geometry import shape, MultiPolygon, Polygon
from shapely.ops import unary_union
import json
import os
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────

DB_CONFIG = {
    "host": os.getenv("POSTGRES_HOST", "localhost"),
    "port": int(os.getenv("POSTGRES_PORT", 5433)),
    "dbname": os.getenv("POSTGRES_DB"),
    "user": os.getenv("POSTGRES_USER"),
    "password": os.getenv("POSTGRES_PASSWORD"),
}

DIFF_RASTER = "data/ndvi_difference_2021_vs_2024.tif"
STATS_JSON = "data/stats_summary.json"
DEGRADATION_THRESHOLD = -0.10

# Área mínima de un polígono para incluirlo (filtra ruido de pixels sueltos)
# 0.5 km² elimina manchas de 1-2 pixels que no tienen significado ecológico real
MIN_AREA_KM2 = 0.5


# ─────────────────────────────────────────────
# SECCIÓN 1: VECTORIZACIÓN DEL RASTER
# ─────────────────────────────────────────────

def vectorize_degradation(raster_path, threshold):
    """
    Convierte los píxeles de degradación severa en polígonos vectoriales.

    rasterio.features.shapes() es el corazón de esta operación:
    - Recibe un array 2D y una máscara booleana
    - Agrupa píxeles contiguos del mismo valor
    - Devuelve sus contornos como geometrías GeoJSON
    - Aplica la transformación afín (transform) para convertir coordenadas
      de píxel (fila, columna) a coordenadas geográficas reales (lon, lat)

    ¿Por qué convertimos el array a int16?
    rasterio.features.shapes() requiere un array de enteros o uint8/int16.
    Nuestro array de NDVI es float32 - lo binarizamos primero (0/1) y luego
    lo casteamos a int16 para que shapes() lo acepte.
    """
    with rasterio.open(raster_path) as src:
        ndvi_diff = src.read(1).astype(np.float32)
        transform = src.transform
        crs = src.crs

        # Máscara binaria: 1 donde hay degradación, 0 en el resto
        degraded_mask = (ndvi_diff < threshold).astype(np.int16)

        # NoData original (bordes fuera de Catalunya) también lo excluimos
        nodata_mask = (ndvi_diff != src.nodata) if src.nodata else np.ones_like(degraded_mask, dtype=bool)

        print(f"Píxeles totales válidos : {np.sum(nodata_mask):,}")
        print(f"Píxeles de degradación : {np.sum(degraded_mask):,}")

        # shapes() devuelve un iterador de (geojson_geometry_dict, pixel_value)
        # Solo nos interesan los shapes con valor 1 (degradación)
        shapes_gen = rasterio.features.shapes(
            degraded_mask,
            mask=degraded_mask.astype(np.uint8), # solo vectoriza donde hay 1s
            transform=transform,
            connectivity=8, # conectividad 8: los píxeles diagonales también se unen
                            # conectividad 4 crearía muchos polígonos fragmentados
        )

        polygons = []
        pixel_area_km2 = (abs(transform.a) * 111) * (abs(transform.e) * 111)

        for geom_dict, value in shapes_gen:
            if value != 1:
                continue

            geom = shape(geom_dict)  # convierte el dict GeoJSON en objeto Shapely

            # Simplificamos ligeramente la geometría para reducir vértices redundantes
            # tolerance en grados: 0.001° ≈ 100m a latitud 41°N
            geom = geom.simplify(tolerance=0.001, preserve_topology=True)

            # Calculamos área en km² usando la misma aproximación que en analytics.py
            pixel_count = int(round(geom.area / (transform.a * abs(transform.e))))
            area_km2 = pixel_count * pixel_area_km2

            if area_km2 < MIN_AREA_KM2:
                continue  # descartamos manchas de ruido

            polygons.append({
                "geometry": geom,
                "area_km2": round(area_km2, 3),
                "pixel_count": pixel_count,
            })

    print(f"Polígonos generados (área ≥ {MIN_AREA_KM2} km²): {len(polygons)}")
    return polygons, crs


# ─────────────────────────────────────────────
# SECCIÓN 2: CARGA EN POSTGIS
# ─────────────────────────────────────────────

def get_db_connection():
    """Devuelve una conexión psycopg2. Falla rápido con mensaje claro si no puede conectar."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        print(f"  Conectado a PostgreSQL en {DB_CONFIG['host']}:{DB_CONFIG['port']}")
        return conn
    except psycopg2.OperationalError as e:
        raise ConnectionError(
            f"No se pudo conectar a PostgreSQL.\n"
            f"¿Está el contenedor corriendo? Ejecuta: docker compose ps\n"
            f"Error original: {e}"
        )


def insert_analysis_run(conn, stats):
    """
    Inserta un registro en analysis_runs y retorna el ID generado.
    RETURNING id es sintaxis PostgreSQL que devuelve el ID del INSERT
    sin necesitar un SELECT separado - más eficiente y atómico.
    """
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO analysis_runs (
                period_baseline, period_compare, region_name,
                ndvi_mean_baseline, ndvi_mean_compare, ndvi_mean_diff,
                pct_degraded, area_degraded_km2, pct_improved, area_improved_km2
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            "verano_2021", "verano_2024", "Catalunya",
            stats["ndvi_mean_2021"], stats["ndvi_mean_2024"], stats["ndvi_mean_diff"],
            stats["pct_degraded"], stats["area_degraded_km2"],
            stats["pct_improved"], stats["area_improved_km2"],
        ))
        run_id = cur.fetchone()[0]
    conn.commit()
    print(f"Run registrado en analysis_runs con id={run_id}")
    return run_id


def insert_degradation_zones(conn, run_id, polygons):
    """
    Inserta todos los polígonos de degradación en un solo batch.

    execute_values() de psycopg2 hace un INSERT múltiple eficiente.
    Un INSERT por polígono sería ~100x más lento para miles de filas.

    ST_GeomFromText(wkt, 4326) convierte texto WKT a geometría PostGIS
    con el CRS correcto declarado. wkt = Well-Known Text, el formato
    estándar ISO para representar geometrías como texto legible.

    Convertimos cada Polygon suelto en MultiPolygon para consistencia
    de tipos con la definición de la columna en init.sql.
    """
    rows = []
    for p in polygons:
        geom = p["geometry"]
        # Normalizar a MultiPolygon
        if isinstance(geom, Polygon):
            geom = MultiPolygon([geom])
        rows.append((
            run_id,
            "severe",
            p["area_km2"],
            p["pixel_count"],
            geom.wkt,
        ))

    with conn.cursor() as cur:
        execute_values(cur, """
            INSERT INTO degradation_zones (run_id, zone_type, area_km2, pixel_count, geom)
            VALUES %s
        """,
        rows,
        template="(%s, %s, %s, %s, ST_GeomFromText(%s, 4326))"
        )
    conn.commit()
    print(f"{len(rows)} polígonos insertados en degradation_zones")


def verify_insertion(conn, run_id):
    """
    Consulta de verificación con funciones PostGIS reales.
    ST_Area() con cast a geography calcula el área en metros² sobre
    el elipsoide real (no en grados²) — mucho más preciso para áreas grandes.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                COUNT(*) AS total_zonas,
                ROUND(SUM(area_km2)::numeric, 1) AS area_total_km2,
                ROUND(AVG(pixel_count)::numeric, 0) AS media_pixels_por_zona,
                ROUND(MAX(area_km2)::numeric, 1) AS zona_mas_grande_km2
            FROM degradation_zones
            WHERE run_id = %s
        """, (run_id,))
        row = cur.fetchone()

    print("\n── Verificación en PostGIS ────────")
    print(f"Zonas almacenadas: {row[0]}")
    print(f"Área total degradada: {row[1]} km²")
    print(f"Media píxeles por zona: {row[2]}")
    print(f"Zona más grande: {row[3]} km²")
    print("─────────────────────────────────────")


# ─────────────────────────────────────────────
# PUNTO DE ENTRADA
# ─────────────────────────────────────────────

def main():
    print("\n" + "=" * 55)
    print("INGESTA EN POSTGIS - ZONAS DE DEGRADACIÓN")
    print("=" * 55)

    # 1. Leer estadísticas generadas en el Hito 2
    print("\n[1/4] Leyendo stats_summary.json...")
    with open(STATS_JSON, encoding="utf-8") as f:
        stats = json.load(f)
    print(f"Degradación registrada: {stats['pct_degraded']}% · {stats['area_degraded_km2']} km²")

    # 2. Vectorizar el raster de diferencia
    print("\n[2/4] Vectorizando zonas de degradación severa...")
    polygons, crs = vectorize_degradation(DIFF_RASTER, DEGRADATION_THRESHOLD)

    # 3. Conectar e insertar
    print("\n[3/4] Conectando a PostgreSQL...")
    conn = get_db_connection()

    print("\n[4/4] Insertando en PostGIS...")
    run_id = insert_analysis_run(conn, stats)
    insert_degradation_zones(conn, run_id, polygons)
    verify_insertion(conn, run_id)

    conn.close()


if __name__ == "__main__":
    main()