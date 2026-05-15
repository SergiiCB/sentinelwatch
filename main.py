# ─────────────────────────────────────────────
# Hito 1: Orquestador GEE
# ─────────────────────────────────────────────

import os
from config import initialize_gee, AOI, PERIOD_BASELINE, PERIOD_COMPARE, MAX_CLOUD_COVER, EXPORT_SCALE
from gee_extractor import get_composite_ndvi, export_ndvi_image, calculate_ndvi_difference

def main():
    # --- 1. Obtener NDVI compuesto para cada periodo ---
    print(f"\n[1/4] Procesando periodo base: {PERIOD_BASELINE['name']}")
    ndvi_baseline = get_composite_ndvi(
        aoi=AOI,
        start_date=PERIOD_BASELINE["start"],
        end_date=PERIOD_BASELINE["end"],
        max_cloud_cover=MAX_CLOUD_COVER,
    )

    print(f"\n[2/4] Procesando periodo comparación: {PERIOD_COMPARE['name']}")
    ndvi_compare = get_composite_ndvi(
        aoi=AOI,
        start_date=PERIOD_COMPARE["start"],
        end_date=PERIOD_COMPARE["end"],
        max_cloud_cover=MAX_CLOUD_COVER,
    )

    # --- 2. Calcular diferencia ---
    print("\n[3/4] Calculando diferencia NDVI (cambio de vegetación)...")
    ndvi_diff = calculate_ndvi_difference(ndvi_baseline, ndvi_compare)
    
    # --- 3. Exportar los tres GeoTIFFs ---
    # Exportamos los tres: baseline, comparación y diferencia.
    # El Hito 2 consumirá estos archivos para análisis estadístico.
    print("\n[4/4] Exportando GeoTIFFs...")
    
    export_ndvi_image(
        ndvi_baseline,
        filename=f"data/{PERIOD_BASELINE['name']}_ndvi.tif",
        aoi=AOI,
        scale=EXPORT_SCALE,
    )
    export_ndvi_image(
        ndvi_compare,
        filename=f"data/{PERIOD_COMPARE['name']}_ndvi.tif",
        aoi=AOI,
        scale=EXPORT_SCALE,
    )
    export_ndvi_image(
        ndvi_diff,
        filename="data/ndvi_difference_2021_vs_2024.tif",
        aoi=AOI,
        scale=EXPORT_SCALE,
    )

    print("\nCompletado. Archivos en /data:")
    print(f"• data/{PERIOD_BASELINE['name']}_ndvi.tif")
    print(f"• data/{PERIOD_COMPARE['name']}_ndvi.tif")
    print(f"• data/ndvi_difference_2021_vs_2024.tif")


if __name__ == "__main__":
    main()