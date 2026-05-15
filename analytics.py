# ─────────────────────────────────────────────
# Hito 2: Análisis NDVI y visualización
# ─────────────────────────────────────────────

import numpy as np
import rasterio
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from matplotlib.patches import Patch
import os

# ─────────────────────────────────────────────
# SECCIÓN 1: LECTURA DE DATOS
# ─────────────────────────────────────────────
# rasterio lee GeoTIFFs y nos da dos cosas:
# 1. El array NumPy con los valores de pixel (el "qué").
# 2. Los metadatos espaciales: CRS, transform, resolución (el "dónde").
#
# ¿Por qué guardamos 'transform' y 'crs'?
# Porque cuando dibujemos el mapa, necesitamos saber cuántos metros
# representa cada pixel para calcular áreas reales en km².
# Sin metadatos espaciales, solo tenemos una imagen sin georeferencia.
# ─────────────────────────────────────────────

DATA_DIR = "data"

def load_raster(filepath):
    """
    Lee un GeoTIFF y retorna el array de datos con sus metadatos.
    
    El 'with' garantiza que el archivo se cierra aunque ocurra un error.
    src.read(1) lee la banda 1 (base 1, no base 0 - peculiaridad de rasterio).
    masked=True convierte los píxeles NoData en np.nan automáticamente.
    Sin esto, zonas fuera de Catalunya (mar, Francia) contaminarían las estadísticas.
    """
    with rasterio.open(filepath) as src:
        data = src.read(1, masked=True).astype(np.float32)
        # Convertimos la máscara a NaN para que NumPy la ignore en cálculos
        data = np.where(src.read_masks(1) == 0, np.nan, data)
        meta = {
            "transform": src.transform,
            "crs": src.crs,
            "width": src.width,
            "height": src.height,
            # pixel_area_km2: área real de cada pixel en km²
            # src.res retorna (ancho_pixel, alto_pixel) en las unidades del CRS.
            # Con EPSG:4326 las unidades son GRADOS, no metros - necesitamos
            # convertir. Para latitud ~41°N, 1 grado ≈ 111km.
            # Fórmula: (grados * 111km)² = km² por pixel
            "pixel_area_km2": (abs(src.res[0]) * 111) * (abs(src.res[1]) * 111),
        }
    return data, meta


# ─────────────────────────────────────────────
# SECCIÓN 2: ANÁLISIS ESTADÍSTICO
# ─────────────────────────────────────────────
# np.nanmean, np.nanstd, etc. ignoran los NaN que pusimos en la sección anterior.
# Si usáramos np.mean, un solo NaN contaminaría toda la media con NaN.
# ─────────────────────────────────────────────

DEGRADATION_THRESHOLD = -0.10 # Umbral estándar en teledetección para pérdida significativa

def compute_statistics(ndvi_2021, ndvi_2024, ndvi_diff, pixel_area_km2):
    """
    Calcula las métricas clave del análisis de cambio de vegetación.
    Retorna un diccionario con todos los resultados para usarlos
    tanto en el print() de consola como en el mapa (Hito 4: informe LLM).
    """
    # Máscaras booleanas para cada categoría de cambio
    # np.nan comparado con cualquier número siempre da False → los NaN quedan excluidos
    mask_degraded = ndvi_diff < DEGRADATION_THRESHOLD # pérdida significativa
    mask_improved = ndvi_diff > abs(DEGRADATION_THRESHOLD) # mejora significativa
    mask_stable = ~mask_degraded & ~mask_improved & ~np.isnan(ndvi_diff)

    total_valid_pixels = np.sum(~np.isnan(ndvi_diff))

    stats = {
        # Medias generales
        "ndvi_mean_2021": round(float(np.nanmean(ndvi_2021)), 4),
        "ndvi_mean_2024": round(float(np.nanmean(ndvi_2024)), 4),
        "ndvi_mean_diff": round(float(np.nanmean(ndvi_diff)), 4),

        # Área afectada
        "pixels_degraded": int(np.sum(mask_degraded)),
        "pixels_improved": int(np.sum(mask_improved)),
        "pixels_stable": int(np.sum(mask_stable)),
        "total_valid_pixels": int(total_valid_pixels),

        "pct_degraded": round(100 * np.sum(mask_degraded) / total_valid_pixels, 2),
        "pct_improved": round(100 * np.sum(mask_improved) / total_valid_pixels, 2),
        "pct_stable": round(100 * np.sum(mask_stable) / total_valid_pixels, 2),

        # Áreas en km² reales (cada pixel = pixel_area_km2 km²)
        "area_degraded_km2": round(np.sum(mask_degraded) * pixel_area_km2, 1),
        "area_improved_km2": round(np.sum(mask_improved) * pixel_area_km2, 1),

        # Variabilidad: std alta = cambios heterogéneos (zonas muy afectadas junto a zonas no)
        "ndvi_std_2021": round(float(np.nanstd(ndvi_2021)), 4),
        "ndvi_std_2024": round(float(np.nanstd(ndvi_2024)), 4),
    }
    return stats


def print_report(stats):
    """Imprime un resumen legible en consola. Útil para debugging rápido."""
    print("\n" + "=" * 55)
    print("ANÁLISIS DE VEGETACIÓN — CATALUNYA 2021 vs 2024")
    print("=" * 55)
    print(f"NDVI medio 2021 : {stats['ndvi_mean_2021']:+.4f}  (±{stats['ndvi_std_2021']:.4f})")
    print(f"NDVI medio 2024 : {stats['ndvi_mean_2024']:+.4f}  (±{stats['ndvi_std_2024']:.4f})")
    print(f"Cambio medio : {stats['ndvi_mean_diff']:+.4f}")
    print("-" * 55)
    print(f"Degradación significativa (NDVI < {DEGRADATION_THRESHOLD}):")
    print(f"{stats['pct_degraded']:5.1f}% del área  ({stats['area_degraded_km2']:,.0f} km²)")
    print(f"Mejora significativa:")
    print(f"{stats['pct_improved']:5.1f}% del área  ({stats['area_improved_km2']:,.0f} km²)")
    print(f"Sin cambio significativo:")
    print(f"{stats['pct_stable']:5.1f}% del área")
    print("=" * 55)


# ─────────────────────────────────────────────
# SECCIÓN 3: VISUALIZACIÓN
# ─────────────────────────────────────────────
# Generamos una figura con 3 subplots lado a lado:
#   [NDVI 2021]  [NDVI 2024]  [Diferencia + máscara de degradación]
#
# ¿Por qué RdYlGn para NDVI y RdBu para la diferencia?
# RdYlGn (rojo-amarillo-verde) es intuitivo para vegetación: rojo=suelo desnudo,
# verde=bosque denso. Es el estándar en publicaciones de teledetección.
# RdBu (rojo-blanco-azul) con vmin/vmax centrado en 0 hace que el blanco
# represente "sin cambio", el rojo "pérdida" y el azul "ganancia".
# diverge_norm centra el colormap en 0 aunque los valores min/max no sean simétricos.
# ─────────────────────────────────────────────

def plot_analysis(ndvi_2021, ndvi_2024, ndvi_diff, stats, output_path="data/analisis_catalunya.png"):
    """
    Genera y guarda la figura de análisis con 3 paneles.
    """
    fig, axes = plt.subplots(1, 3, figsize=(18, 7))
    fig.patch.set_facecolor("#1a1a2e") # fondo oscuro - los mapas de vegetación destacan más

    common_kwargs = dict(
        cmap="RdYlGn",
        vmin=-0.2, # valores por debajo de -0.2 = suelo desnudo / roca
        vmax=0.8, # valores por encima de 0.8 = vegetación muy densa
        interpolation="nearest",
    )

    # ── Panel 1: NDVI 2021 (baseline) ──
    ax1 = axes[0]
    im1 = ax1.imshow(ndvi_2021, **common_kwargs)
    ax1.set_title("NDVI Verano 2021\n(línea base)", color="white", fontsize=12, pad=10)
    ax1.axis("off")
    cbar1 = plt.colorbar(im1, ax=ax1, fraction=0.046, pad=0.04)
    cbar1.set_label("NDVI", color="white", fontsize=9)
    cbar1.ax.yaxis.set_tick_params(color="white")
    plt.setp(cbar1.ax.yaxis.get_ticklabels(), color="white")
    # Anotamos la media directamente en el mapa
    ax1.text(0.02, 0.02, f"μ = {stats['ndvi_mean_2021']:.3f}",
             transform=ax1.transAxes, color="white", fontsize=10,
             bbox=dict(boxstyle="round,pad=0.3", facecolor="black", alpha=0.5))

    # ── Panel 2: NDVI 2024 (comparación) ──
    ax2 = axes[1]
    im2 = ax2.imshow(ndvi_2024, **common_kwargs)
    ax2.set_title("NDVI Verano 2024\n(comparación)", color="white", fontsize=12, pad=10)
    ax2.axis("off")
    cbar2 = plt.colorbar(im2, ax=ax2, fraction=0.046, pad=0.04)
    cbar2.set_label("NDVI", color="white", fontsize=9)
    cbar2.ax.yaxis.set_tick_params(color="white")
    plt.setp(cbar2.ax.yaxis.get_ticklabels(), color="white")
    ax2.text(0.02, 0.02, f"μ = {stats['ndvi_mean_2024']:.3f}",
             transform=ax2.transAxes, color="white", fontsize=10,
             bbox=dict(boxstyle="round,pad=0.3", facecolor="black", alpha=0.5))

    # ── Panel 3: Diferencia + máscara de degradación ──
    ax3 = axes[2]
    
    # Colormap divergente centrado en 0
    divnorm = mcolors.TwoSlopeNorm(vmin=-0.4, vcenter=0, vmax=0.4)
    im3 = ax3.imshow(ndvi_diff, cmap="RdBu", norm=divnorm, interpolation="nearest")
    
    # Superponemos la máscara de degradación severa en naranja semitransparente
    # np.where devuelve un array con 1 donde hay degradación y NaN donde no.
    # imshow con alpha=0.6 lo pone encima del mapa de diferencia como overlay.
    degraded_overlay = np.where(ndvi_diff < DEGRADATION_THRESHOLD, 1.0, np.nan)
    ax3.imshow(degraded_overlay, cmap="hot", vmin=0, vmax=2,
               interpolation="nearest", alpha=0.55)

    ax3.set_title(f"Cambio NDVI (2024 − 2021)\nDegradación: {stats['pct_degraded']}% del área",
                  color="white", fontsize=12, pad=10)
    ax3.axis("off")
    cbar3 = plt.colorbar(im3, ax=ax3, fraction=0.046, pad=0.04)
    cbar3.set_label("ΔNDVI", color="white", fontsize=9)
    cbar3.ax.yaxis.set_tick_params(color="white")
    plt.setp(cbar3.ax.yaxis.get_ticklabels(), color="white")

    # Leyenda manual para el overlay naranja
    legend_elements = [
        Patch(facecolor="orangered", alpha=0.7, label=f"Degradación severa (ΔNDVI < {DEGRADATION_THRESHOLD})"),
        Patch(facecolor="#4575b4",   alpha=0.9, label="Mejora / recuperación"),
        Patch(facecolor="#d73027",   alpha=0.9, label="Pérdida moderada"),
    ]
    ax3.legend(handles=legend_elements, loc="lower left",
               facecolor="#1a1a2e", edgecolor="gray",
               labelcolor="white", fontsize=8)

    # ── Título global y créditos ──
    fig.suptitle(
        "Impacto de la Sequía en la Vegetación de Catalunya\n"
        "Análisis comparativo Sentinel-2 · Verano 2021 vs Verano 2024",
        color="white", fontsize=14, fontweight="bold", y=1.01
    )
    fig.text(0.5, -0.01,
             "Fuente: Copernicus/Sentinel-2 SR Harmonized · Google Earth Engine · "
             f"Resolución: 100m/px · Umbral degradación: ΔNDVI < {DEGRADATION_THRESHOLD}",
             ha="center", color="#888888", fontsize=8)

    plt.tight_layout()
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    plt.savefig(output_path, dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    print(f"\nMapa guardado en: {output_path}")
    plt.show()


# ─────────────────────────────────────────────
# PUNTO DE ENTRADA
# ─────────────────────────────────────────────

def main():
    print("\n[1/3] Cargando GeoTIFFs...")
    ndvi_2021, meta_2021 = load_raster(os.path.join(DATA_DIR, "verano_2021_ndvi.tif"))
    ndvi_2024, meta_2024 = load_raster(os.path.join(DATA_DIR, "verano_2024_ndvi.tif"))
    ndvi_diff, meta_diff = load_raster(os.path.join(DATA_DIR, "ndvi_difference_2021_vs_2024.tif"))
    print(f"Arrays cargados - shape: {ndvi_diff.shape}  "
          f"({ndvi_diff.shape[1] * meta_diff['pixel_area_km2']:.0f} km² de ancho aprox.)")

    print("\n[2/3] Calculando estadísticas...")
    stats = compute_statistics(ndvi_2021, ndvi_2024, ndvi_diff, meta_diff["pixel_area_km2"])
    print_report(stats)

    print("\n[3/3] Generando mapa visual...")
    plot_analysis(ndvi_2021, ndvi_2024, ndvi_diff, stats)

    # Guardamos las estadísticas en JSON para que el agente LLM del Hito 4 las lea
    import json
    stats_path = os.path.join(DATA_DIR, "stats_summary.json")
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)
    print(f"Estadísticas guardadas en: {stats_path}")


if __name__ == "__main__":
    main()