# ─────────────────────────────────────────────
# Hito 5: Dashboard Streamlit
# ─────────────────────────────────────────────

# Ejecutar con:  streamlit run app.py

# Dos pestañas:
#   1. Mapa interactivo de degradación (Folium sobre Leaflet.js).
#   2. Último informe ejecutivo generado por el LLM local.

import os
import glob
import numpy as np
import streamlit as st
import folium
from streamlit_folium import st_folium
import rasterio
import rasterio.features
from shapely.geometry import shape, mapping
import json
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

# ─────────────────────────────────────────────
# CONFIGURACIÓN DE PÁGINA
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="Monitor de Vegetación · Catalunya",
    page_icon="🌿",
    layout="wide",
    initial_sidebar_state="expanded",
)

DATA_DIR              = "data"
DIFF_RASTER           = os.path.join(DATA_DIR, "ndvi_difference_2021_vs_2024.tif")
STATS_JSON            = os.path.join(DATA_DIR, "stats_summary.json")
DEGRADATION_THRESHOLD = -0.10
MAP_CENTER            = [41.7, 1.8]   # centro geográfico de Catalunya
MAP_ZOOM              = 7


# ─────────────────────────────────────────────
# CARGA DE DATOS
# @st.cache_data evita releer los archivos en cada rerender de Streamlit.
# Sin caché, cada clic del usuario relanzaría la lectura del GeoTIFF completo.
# ─────────────────────────────────────────────

@st.cache_data(show_spinner="Leyendo GeoTIFF y vectorizando zonas de degradación...")
def load_diff_raster(path):
    """
    Lee el raster de diferencia NDVI y vectoriza las zonas de degradación.
    Retorna una lista de dicts ordenada por área descendente.
    """
    zones = []
    with rasterio.open(path) as src:
        diff_array = src.read(1).astype(np.float32)
        transform  = src.transform
        pixel_area_km2 = (abs(transform.a) * 111) * (abs(transform.e) * 111)

        mask = (diff_array < DEGRADATION_THRESHOLD).astype(np.uint8)

        for geom_dict, val in rasterio.features.shapes(
            mask, mask=mask, transform=transform, connectivity=8
        ):
            if val != 1:
                continue
            geom     = shape(geom_dict).simplify(0.002, preserve_topology=True)
            area_km2 = round(
                geom.area / (transform.a * abs(transform.e)) * pixel_area_km2, 2
            )
            if area_km2 < 1.0:
                continue
            zones.append({
                "geometry":     mapping(geom),
                "area_km2":     area_km2,
                "centroid_lat": round(geom.centroid.y, 4),
                "centroid_lon": round(geom.centroid.x, 4),
            })

    zones.sort(key=lambda z: z["area_km2"], reverse=True)
    return zones


@st.cache_data(show_spinner="Cargando estadísticas...")
def load_stats(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def find_latest_report(data_dir):
    """Encuentra el informe .md más reciente generado por el Hito 4."""
    files = glob.glob(os.path.join(data_dir, "informe_*.md"))
    if not files:
        return None, None
    latest = max(files, key=os.path.getmtime)
    with open(latest, encoding="utf-8") as f:
        return latest, f.read()


# ─────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────

def render_sidebar(stats):
    st.sidebar.title("🌿 Monitor Vegetación")
    st.sidebar.caption("Sentinel-2 · GEE · PostGIS · DeepSeek-R1")
    st.sidebar.divider()

    if stats:
        st.sidebar.subheader("Métricas del análisis")
        st.sidebar.metric("NDVI medio 2021", f"{stats['ndvi_mean_2021']:.4f}")
        st.sidebar.metric(
            "NDVI medio 2024",
            f"{stats['ndvi_mean_2024']:.4f}",
            delta=f"{stats['ndvi_mean_diff']:+.4f}",
            delta_color="inverse",
        )
        st.sidebar.divider()
        st.sidebar.metric(
            "Área degradada",
            f"{stats['area_degraded_km2']:,.0f} km²",
            delta=f"{stats['pct_degraded']}% del territorio",
            delta_color="inverse",
        )
        st.sidebar.metric(
            "Área en recuperación",
            f"{stats['area_improved_km2']:,.0f} km²",
            delta=f"{stats['pct_improved']}% del territorio",
            delta_color="normal",
        )
    else:
        st.sidebar.warning(
            "No se encontró `stats_summary.json`.\n\n"
            "Ejecuta primero `python analytics.py`."
        )

    st.sidebar.divider()
    st.sidebar.info(
        "**🔒 IA Soberana · Coste $0**\n\n"
        "Inferencia 100% local con LM Studio.\n"
        "Modelo: DeepSeek-R1-8B\n"
        "Hardware: RTX 3060 Ti\n\n"
        "Ningún dato sale de tu máquina."
    )


# ─────────────────────────────────────────────
# PESTAÑA 1: MAPA INTERACTIVO
# ─────────────────────────────────────────────

def render_map_tab(zones, stats):
    st.header("🗺️ Mapa de degradación de vegetación")
    st.caption(
        "Comparativa verano 2021 vs verano 2024  ·  "
        f"Umbral ΔNDVI < {DEGRADATION_THRESHOLD}  ·  "
        "Fuente: Copernicus Sentinel-2 SR Harmonized"
    )

    if not zones:
        st.error(f"No se pudo leer `{DIFF_RASTER}`. ¿Completaste el Hito 1?")
        return

    # ── Controles ──
    col1, col2, _ = st.columns([1, 1, 2])
    with col1:
        min_area = st.slider(
            "Área mínima (km²)", min_value=1, max_value=200,
            value=5, step=5,
            help="Filtra zonas pequeñas para reducir ruido visual",
        )
    with col2:
        map_style = st.selectbox(
            "Estilo del mapa base",
            ["CartoDB dark_matter", "OpenStreetMap", "CartoDB positron"],
        )

    filtered = [z for z in zones if z["area_km2"] >= min_area]
    st.caption(f"Mostrando **{len(filtered)}** zonas ≥ {min_area} km²")

    # ── Mapa Folium ──
    # Folium construye un mapa Leaflet.js que streamlit-folium embebe como iframe.
    # Cada polígono GeoJSON se añade como una capa vectorial interactiva.
    m = folium.Map(location=MAP_CENTER, zoom_start=MAP_ZOOM, tiles=map_style)

    max_area = max((z["area_km2"] for z in filtered), default=1)

    for zone in filtered:
        # Opacidad proporcional al área: zonas grandes destacan más
        opacity = 0.30 + 0.50 * (zone["area_km2"] / max_area)

        folium.GeoJson(
            data={"type": "Feature", "geometry": zone["geometry"]},
            style_function=lambda feat, op=opacity: {
                "fillColor":"#d73027",
                "color":"#a50026",
                "weight":0.8,
                "fillOpacity":op,
            },
            tooltip=folium.Tooltip(
                f"<b>Zona degradada</b><br>"
                f"Área: <b>{zone['area_km2']} km²</b><br>"
                f"Centro: {zone['centroid_lat']}°N, {zone['centroid_lon']}°E",
                sticky=True,
            ),
            popup=folium.Popup(
                f"<div style='font-size:13px'>"
                f"<b style='color:#d73027'>⚠ Degradación severa</b><br><br>"
                f"<b>Área:</b> {zone['area_km2']} km²<br>"
                f"<b>Lat:</b> {zone['centroid_lat']}°N<br>"
                f"<b>Lon:</b> {zone['centroid_lon']}°E"
                f"</div>",
                max_width=200,
            ),
        ).add_to(m)

    # Marcador en la zona más crítica
    if filtered:
        top = filtered[0]
        folium.Marker(
            location=[top["centroid_lat"], top["centroid_lon"]],
            tooltip="⚠ Zona más crítica",
            popup=f"<b>Zona más crítica</b><br>{top['area_km2']} km²",
            icon=folium.Icon(color="red", icon="exclamation-sign", prefix="glyphicon"),
        ).add_to(m)

    # Leyenda superpuesta
    m.get_root().html.add_child(folium.Element("""
    <div style="position:fixed;bottom:30px;left:30px;z-index:1000;
                background:rgba(15,15,25,0.88);padding:12px 16px;
                border-radius:8px;color:white;font-size:12px;
                border:1px solid rgba(255,255,255,0.15)">
        <b>Leyenda</b><br><br>
        <span style="color:#d73027">■</span> Degradación severa (ΔNDVI &lt; -0.10)<br>
        <span style="color:#888">■</span> Opacidad ∝ área de la zona<br>
        <span style="color:red">●</span> Zona más crítica
    </div>
    """))

    st_folium(m, width="100%", height=560, returned_objects=[])

    # ── Tabla Top 10 ──
    st.subheader("Top 10 zonas por extensión")
    import pandas as pd
    st.dataframe(
        pd.DataFrame([{
            "Rank": i + 1,
            "Área (km²)": z["area_km2"],
            "Latitud": z["centroid_lat"],
            "Longitud": z["centroid_lon"],
        } for i, z in enumerate(filtered[:10])]),
        use_container_width=True,
        hide_index=True,
    )


# ─────────────────────────────────────────────
# PESTAÑA 2: INFORME IA
# ─────────────────────────────────────────────

def render_report_tab():
    st.header("📄 Informe ejecutivo generado por IA local")

    report_path, report_content = find_latest_report(DATA_DIR)

    if not report_content:
        st.warning(
            "No se encontró ningún informe en `data/informe_*.md`.\n\n"
            "Ejecuta el Hito 4 primero:\n```bash\npython report_agent.py\n```"
        )
        return

    mod_time = datetime.fromtimestamp(os.path.getmtime(report_path))
    c1, c2, c3 = st.columns(3)
    c1.metric("Generado", mod_time.strftime("%d/%m/%Y %H:%M"))
    c2.metric("Modelo", "DeepSeek-R1-8B")
    c3.metric("Inferencia", "Local · LM Studio · RTX 3060 Ti")
    st.caption(f"Archivo: `{os.path.basename(report_path)}`")
    st.divider()

    # Eliminamos el frontmatter YAML antes de renderizar el Markdown
    content = report_content
    if content.startswith("---"):
        end = content.find("---", 3)
        if end != -1:
            content = content[end + 3:].strip()

    st.markdown(content, unsafe_allow_html=False)
    st.divider()

    st.download_button(
        label="⬇️ Descargar informe (.md)",
        data=report_content,
        file_name=os.path.basename(report_path),
        mime="text/markdown",
    )

    with st.expander("ℹ️ Sobre la IA que generó este informe"):
        st.markdown("""
**Modelo:** DeepSeek-R1-8B (destilado de DeepSeek-R1)
**Servidor de inferencia:** LM Studio (local)
**Hardware:** NVIDIA RTX 3060 Ti · 8GB VRAM
**Coste de inferencia:** $0.00

**Datos proporcionados al modelo (solo estos):**
- Estadísticas globales de NDVI (2021 vs 2024)
- Top 5 zonas de degradación con coordenadas y áreas
- Contexto geográfico de cada comarca

**IA Soberana:** toda la inferencia ocurre en hardware local, sin enviar datos
a servidores de terceros. Apropiado para proyectos con restricciones de privacidad
o para entornos con conectividad limitada.
        """)


# ─────────────────────────────────────────────
# PUNTO DE ENTRADA
# ─────────────────────────────────────────────

def main():
    stats = load_stats(STATS_JSON)
    render_sidebar(stats)

    st.title("🛰️ Sistema de Monitorización de Vegetación · Catalunya")
    st.markdown(
        "Análisis de impacto de sequía 2021–2024 · "
        "**Sentinel-2 SR** · **Google Earth Engine** · **PostGIS** · **IA local**"
    )
    st.divider()

    tab_map, tab_report = st.tabs(["🗺️ Mapa interactivo", "📄 Informe IA"])

    with tab_map:
        if os.path.exists(DIFF_RASTER):
            zones = load_diff_raster(DIFF_RASTER)
            render_map_tab(zones, stats)
        else:
            st.error(
                f"`{DIFF_RASTER}` no encontrado. "
                "Completa el Hito 1 (`python main.py`) antes de lanzar el dashboard."
            )

    with tab_report:
        render_report_tab()


if __name__ == "__main__":
    main()