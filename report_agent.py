# ─────────────────────────────────────────────
# Hito 4: Agente LLM → informe Markdown
# ─────────────────────────────────────────────

# Patrón de arquitectura: "RAG sin embeddings".
# En vez de buscar documentos por similitud semántica, hacemos una consulta.
# SQL determinista que nos da exactamente los datos que necesitamos.
# El LLM solo hace lo que hace bien, razonar y redactar sobre datos estructurados.

import os
import json
import psycopg2
from psycopg2.extras import RealDictCursor
from openai import OpenAI
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

# ─────────────────────────────────────────────
# SECCIÓN 1: CONFIGURACIÓN DE CLIENTES
# ─────────────────────────────────────────────

def get_db_connection():
    """Reutilizamos la misma lógica de conexión del Hito 3."""
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", 5433)),
        dbname=os.getenv("POSTGRES_DB"),
        user=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD"),
    )


def get_llm_client():
    """
    Crea un cliente OpenAI apuntando a LM Studio en lugar de a api.openai.com.

    ¿Por qué funciona esto? La librería `openai` de Python es agnóstica al servidor,
    solo necesita una URL base y una API key (LM Studio acepta cualquier string,
    no valida la key, ponemos "lm-studio" por convención).

    base_url DEBE terminar en /v1 — es el prefijo del protocolo OpenAI-compatible.
    """
    return OpenAI(
        base_url=os.getenv("LM_STUDIO_URL", "http://localhost:1234/v1"),
        api_key="lm-studio",   # LM Studio no valida esto, pero la librería lo exige
    )


# ─────────────────────────────────────────────
# SECCIÓN 2: EXTRACCIÓN DE DATOS DE POSTGIS
# ─────────────────────────────────────────────

def fetch_analysis_context(conn):
    """
    Extrae de PostGIS todo lo que el LLM necesita saber para redactar el informe.

    Usamos RealDictCursor para que psycopg2 devuelva dicts {columna: valor}
    en lugar de tuplas posicionales. Más legible y menos propenso a errores
    cuando añadimos columnas en el futuro.

    ST_X(ST_Centroid(geom)) / ST_Y(...):
    Calcula el centroide de cada polígono y extrae su longitud/latitud.
    El centroide es el "centro de masa" geométrico, útil para dar
    coordenadas representativas de zonas irregulares.

    ST_AsGeoJSON(geom, 4):
    Convierte la geometría a GeoJSON con 4 decimales de precisión (~11m).
    Lo incluimos para que el Hito 5 (si lo construyes) pueda visualizarlo
    directamente en un mapa web sin re-consultar la BD.
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:

        # ── Resumen global del análisis ──
        cur.execute("""
            SELECT
                region_name,
                period_baseline,
                period_compare,
                ROUND(ndvi_mean_baseline::numeric, 4) AS ndvi_mean_baseline,
                ROUND(ndvi_mean_compare::numeric, 4) AS ndvi_mean_compare,
                ROUND(ndvi_mean_diff::numeric, 4) AS ndvi_mean_diff,
                ROUND(pct_degraded::numeric, 2) AS pct_degraded,
                ROUND(area_degraded_km2::numeric, 1) AS area_degraded_km2,
                ROUND(pct_improved::numeric, 2) AS pct_improved,
                ROUND(area_improved_km2::numeric, 1) AS area_improved_km2,
                run_date
            FROM analysis_runs
            ORDER BY run_date DESC
            LIMIT 1
        """)
        run_summary = dict(cur.fetchone())
        # Convertimos el timestamp a string para que JSON lo serialice sin errores
        run_summary["run_date"] = run_summary["run_date"].isoformat()

        # ── Top 5 zonas de degradación severa ──
        cur.execute("""
            SELECT
                dz.id,
                ROUND(dz.area_km2::numeric, 1) AS area_km2,
                dz.pixel_count,
                ROUND(ST_X(ST_Centroid(dz.geom))::numeric, 4) AS centroid_lon,
                ROUND(ST_Y(ST_Centroid(dz.geom))::numeric, 4) AS centroid_lat,
                -- Bounding box como texto, útil para orientación geográfica
                ROUND(ST_XMin(dz.geom::geometry)::numeric, 3) AS bbox_lon_min,
                ROUND(ST_XMax(dz.geom::geometry)::numeric, 3) AS bbox_lon_max,
                ROUND(ST_YMin(dz.geom::geometry)::numeric, 3) AS bbox_lat_min,
                ROUND(ST_YMax(dz.geom::geometry)::numeric, 3) AS bbox_lat_max
            FROM degradation_zones dz
            JOIN analysis_runs ar ON dz.run_id = ar.id
            WHERE ar.region_name = 'Catalunya'
              AND dz.zone_type = 'severe'
            ORDER BY dz.area_km2 DESC
            LIMIT 5
        """)
        top_zones = [dict(row) for row in cur.fetchall()]

    return run_summary, top_zones


def enrich_zones_with_context(zones):
    """
    Añade contexto geográfico aproximado a cada zona basándose en coordenadas.

    En un sistema de producción esto llamaría a una API de geocodificación inversa
    (Nominatim, Google Maps). Para mi caso, presupuesto cero, usamos una tabla de referencia
    manual de las comarcas principales de Catalunya.

    ¿Por qué esto importa para el LLM? Un modelo de lenguaje puede razonar mucho
    mejor sobre "Conca de Barberà, zona de viñedos y avellanos" que sobre
    "lat: 41.35, lon: 1.07". Le damos contexto semántico, no solo números.
    """
    # Referencia aproximada: (lat_min, lat_max, lon_min, lon_max, nombre, contexto)
    COMARCA_REFS = [
        (41.8, 42.9, 0.7, 3.3, "Pirineu / Prepirineu", "zona de alta montaña, bosques de coníferas y prados alpinos"),
        (41.3, 41.8, 1.5, 2.5, "Plana de Vic / Osona", "zona agrícola de interior con cereales y ganadería"),
        (41.1, 41.5, 0.5, 1.5, "Conca de Barberà / Urgell", "zona de viñedos, avellanos y olivares - cultivos vulnerables a sequía"),
        (40.5, 41.2, 0.1, 1.0, "Terra Alta / Ribera d'Ebre", "zona semiárida, olivares y almendros, históricamente afectada por sequías"),
        (41.2, 41.7, 2.5, 3.3, "Selva / La Garrotxa", "zona volcánica con bosques mixtos mediterráneos"),
        (41.5, 42.0, 2.8, 3.3, "Alt Empordà / Baix Empordà", "zona costera con alcornocales y maquis mediterráneo"),
        (41.3, 41.6, 1.9, 2.5, "Bages / Anoia", "zona de transición, viñedos y bosques de encina"),
        (40.6, 41.3, 0.1, 0.8, "Ports / Montsià", "zona de montaña mediterránea con pinos y garriga"),
    ]

    for zone in zones:
        lat = zone["centroid_lat"]
        lon = zone["centroid_lon"]
        zone["region_approx"] = "Zona no identificada en tabla de referencia"
        zone["region_context"] = ""
        for lat_min, lat_max, lon_min, lon_max, name, context in COMARCA_REFS:
            if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
                zone["region_approx"] = name
                zone["region_context"] = context
                break

    return zones


# ─────────────────────────────────────────────
# SECCIÓN 3: CONSTRUCCIÓN DEL PROMPT
# ─────────────────────────────────────────────

SYSTEM_PROMPT = """Eres un experto senior en teledetección satelital, análisis geoespacial y medio ambiente mediterráneo.
Tu especialidad es traducir datos técnicos de índices de vegetación (NDVI) en informes ejecutivos claros y accionables para responsables de política medioambiental, gestores forestales y medios de comunicación especializados.

REGLAS CRÍTICAS DE FORMATO:
- Escribe el informe DIRECTAMENTE en Markdown.
- NO uses bloques de código (evita ```markdown o ```) para envolver el informe.
- No incluyas saludos ni introducciones, empieza directamente con el Título (#).

Cuando redactes informes:
- Usas terminología técnica precisa pero accesible
- Contextualizas los datos con conocimiento del ecosistema mediterráneo y la fenología vegetal
- Identificas patrones de riesgo y haces recomendaciones concretas
- Escribes siempre en español, en formato Markdown limpio y bien estructurado
- Eres riguroso: nunca inventas datos, solo interpretas los que se te proporcionan"""


def build_user_prompt(run_summary, top_zones):
    """
    Construye el mensaje del usuario con los datos estructurados.

    El diseño del prompt sigue el principio de "datos primero, instrucción después":
    1. Proporcionamos los datos brutos como contexto verificable.
    2. Luego pedimos la tarea concreta.

    Esto reduce las alucinaciones, el modelo tiene los números delante
    y no necesita "recordarlos" de su entrenamiento.
    """

    # Tabla Markdown de las zonas para que el LLM la incluya o reformule
    zones_table = "| # | Área (km²) | Comarca aprox. | Coordenadas centroide | Contexto ecológico |\n"
    zones_table += "|---|-----------|----------------|----------------------|--------------------|\n"
    for i, z in enumerate(top_zones, 1):
        zones_table += (
            f"| {i} | {z['area_km2']} km² "
            f"| {z['region_approx']} "
            f"| {z['centroid_lat']}°N, {z['centroid_lon']}°E "
            f"| {z['region_context']} |\n"
        )

    ndvi_change_pct = round(
        (run_summary['ndvi_mean_compare'] - run_summary['ndvi_mean_baseline'])
        / abs(run_summary['ndvi_mean_baseline']) * 100, 1
    ) if run_summary['ndvi_mean_baseline'] != 0 else 0

    prompt = f"""Se te proporcionan datos de un análisis satelital Sentinel-2 sobre la vegetación de Catalunya.

## DATOS DEL ANÁLISIS

**Región analizada:** {run_summary['region_name']}
**Periodo de referencia:** {run_summary['period_baseline']}
**Periodo de comparación:** {run_summary['period_compare']}
**Fecha del análisis:** {run_summary['run_date'][:10]}

**Métricas globales de vegetación:**
- NDVI medio {run_summary['period_baseline']}: {run_summary['ndvi_mean_baseline']}
- NDVI medio {run_summary['period_compare']}: {run_summary['ndvi_mean_compare']}
- Cambio medio de NDVI: {run_summary['ndvi_mean_diff']} ({ndvi_change_pct:+.1f}% respecto al baseline)
- Área con degradación significativa (ΔNDVI < -0.10): **{run_summary['area_degraded_km2']} km² ({run_summary['pct_degraded']}% del territorio)**
- Área con mejora significativa (ΔNDVI > +0.10): {run_summary['area_improved_km2']} km² ({run_summary['pct_improved']}%)

**Top 5 zonas de degradación más severa (por extensión):**

{zones_table}

## TAREA

Redacta un **Informe Ejecutivo de Emergencia Climática** en Markdown con la siguiente estructura exacta:

1. **Resumen Ejecutivo** (3-4 frases): hallazgo principal, magnitud del impacto, urgencia
2. **Análisis Técnico del NDVI**: qué significa el cambio de {run_summary['ndvi_mean_diff']} en términos ecológicos para el ecosistema mediterráneo
3. **Zonas Críticas**: incluye la tabla proporcionada e interpreta geográfica y ecológicamente cada zona
4. **Contexto: La Sequía Mediterránea 2021-2024**: conecta los datos con el fenómeno climático regional conocido
5. **Recomendaciones de Actuación**: 3-5 medidas concretas priorizadas por urgencia
6. **Limitaciones del Análisis**: sé honesto sobre qué no puede confirmarse solo con datos NDVI

El informe debe ser apto para presentar a un comité de gestión forestal o a medios especializados."""

    return prompt


# ─────────────────────────────────────────────
# SECCIÓN 4: GENERACIÓN Y GUARDADO DEL INFORME
# ─────────────────────────────────────────────

def generate_report(llm_client, system_prompt, user_prompt, model_name="deepseek-r1-8b"):
    """
    Llama al LLM local y hace streaming de la respuesta.

    stream=True es importante aquí por dos razones:
    1. DeepSeek-R1 es un modelo de razonamiento, puede tardar 60-90 segundos
       en generar el informe completo. Sin streaming, la terminal parece colgada.
    2. Puedes ver el proceso de razonamiento del modelo en tiempo real,
       lo que es útil para debuggear si el prompt no está funcionando bien.

    ¿Qué es temperature=0.3?
    Escala de 0 a 2. 0 = determinista (siempre la misma respuesta).
    2 = muy creativo/aleatorio. Para informes técnicos, 0.3 da consistencia
    sin ser completamente rígido en el estilo.
    """
    print("\nConectando con LM Studio... ", end="", flush=True)

    try:
        stream = llm_client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=2500,   # suficiente para un informe de ~1500 palabras
            stream=True,
        )
    except Exception as e:
        raise ConnectionError(
            f"No se pudo conectar con LM Studio.\n"
            f"Verifica que el servidor esté corriendo en {os.getenv('LM_STUDIO_URL', 'http://localhost:1234/v1')}\n"
            f"y que el modelo esté cargado en LM Studio.\n"
            f"Error: {e}"
        )

    print("conectado. Generando informe...\n")
    print("─" * 60)

    full_response = []
    for chunk in stream:
        # Cada chunk contiene un fragmento del texto generado
        delta = chunk.choices[0].delta.content
        if delta:
            print(delta, end="", flush=True)  # streaming en tiempo real a la terminal
            full_response.append(delta)

    print("\n" + "─" * 60)
    return "".join(full_response)


def save_report(report_text, run_summary):
    """
    Guarda el informe en Markdown con nombre que incluye los periodos comparados.
    También actualiza el campo 'notes' en analysis_runs con un extracto.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    filename = (
        f"data/informe_{run_summary['region_name'].lower()}_"
        f"{run_summary['period_baseline']}_vs_{run_summary['period_compare']}_"
        f"{timestamp}.md"
    )

    # Cabecera de metadatos al inicio del fichero Markdown
    header = f"""---
generado: {datetime.now().isoformat()}
region: {run_summary['region_name']}
baseline: {run_summary['period_baseline']}
comparacion: {run_summary['period_compare']}
area_degradada_km2: {run_summary['area_degraded_km2']}
pct_degradado: {run_summary['pct_degraded']}
modelo: DeepSeek-R1-8B (LM Studio local)
fuente_datos: Sentinel-2 SR Harmonized / Google Earth Engine
---

"""
    with open(filename, "w", encoding="utf-8") as f:
        f.write(header + report_text)

    print(f"\nInforme guardado en: {filename}")
    return filename


# ─────────────────────────────────────────────
# PUNTO DE ENTRADA
# ─────────────────────────────────────────────

def main():
    print("\n" + "=" * 60)
    print("AGENTE DE INFORMES")
    print("PostGIS - LM Studio (DeepSeek-R1) - Markdown")
    print("=" * 60)

    # 1. Extraer datos de PostGIS
    print("\n[1/4] Conectando a PostGIS y extrayendo datos...")
    conn = get_db_connection()
    run_summary, top_zones = fetch_analysis_context(conn)
    conn.close()
    print(f"Run más reciente: {run_summary['period_baseline']} vs {run_summary['period_compare']}")
    print(f"Zonas críticas recuperadas: {len(top_zones)}")

    # 2. Enriquecer con contexto geográfico
    print("\n[2/4] Enriqueciendo coordenadas con contexto geográfico...")
    top_zones = enrich_zones_with_context(top_zones)
    for z in top_zones:
        print(f"  Zona {z['id']:>4} | {z['area_km2']:>7} km² | {z['region_approx']}")

    # 3. Construir prompt y llamar al LLM
    print("\n[3/4] Construyendo prompt y llamando a LM Studio...")
    user_prompt = build_user_prompt(run_summary, top_zones)
    llm_client  = get_llm_client()
    report_text = generate_report(llm_client, SYSTEM_PROMPT, user_prompt)

    # 4. Guardar informe
    print("\n[4/4] Guardando informe...")
    report_file = save_report(report_text, run_summary)

    print("\nPipeline completo:")
    print("Sentinel-2 → NDVI → PostGIS → LLM → Informe ejecutivo")
    print(f"\nAbre {report_file} en cualquier visor Markdown para ver el resultado.")


if __name__ == "__main__":
    main()