-- ─────────────────────────────────────────────
-- Schema de la base de datos PostGIS
-- ─────────────────────────────────────────────

-- Este script se ejecuta UNA SOLA VEZ al crear el contenedor por primera vez.

-- Activar la extensión PostGIS en la base de datos.
-- Sin esto, PostgreSQL es una BD relacional normal.
-- Con esto, los tipos GEOMETRY y GEOGRAPHY quedan disponibles,
-- junto con funciones como ST_Area(), ST_Intersection(), ST_AsGeoJSON(), etc.
CREATE EXTENSION IF NOT EXISTS postgis;

-- Activar postgis_raster para operaciones raster-vectoriales avanzadas (Hito 4)
CREATE EXTENSION IF NOT EXISTS postgis_raster;

-- ─────────────────────────────────────────────
-- Tabla de análisis: registra cada ejecución del pipeline
-- ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS analysis_runs (
    id              SERIAL PRIMARY KEY,
    run_date        TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    period_baseline VARCHAR(50)  NOT NULL,   -- ej: "verano_2021"
    period_compare  VARCHAR(50)  NOT NULL,   -- ej: "verano_2024"
    region_name     VARCHAR(100) NOT NULL,   -- ej: "Catalunya"
    ndvi_mean_baseline  FLOAT,
    ndvi_mean_compare   FLOAT,
    ndvi_mean_diff      FLOAT,
    pct_degraded        FLOAT,
    area_degraded_km2   FLOAT,
    pct_improved        FLOAT,
    area_improved_km2   FLOAT,
    notes           TEXT
);

-- ─────────────────────────────────────────────
-- Tabla de geometrías: las zonas de degradación como polígonos reales
-- ─────────────────────────────────────────────
-- ¿Por qué GEOMETRY(MULTIPOLYGON, 4326)?
-- GEOMETRY es el tipo espacial de PostGIS.
-- MULTIPOLYGON porque la vectorización producirá múltiples polígonos
-- discontinuos (manchas de degradación esparcidas por Catalunya).
-- 4326 es el EPSG code de WGS84, el mismo CRS que usamos en GEE.
-- Guardar el CRS en la definición de columna permite a PostGIS
-- reprojectar automáticamente si algún día consultas con otro CRS.
CREATE TABLE IF NOT EXISTS degradation_zones (
    id              SERIAL PRIMARY KEY,
    run_id          INTEGER REFERENCES analysis_runs(id) ON DELETE CASCADE,
    zone_type       VARCHAR(50) NOT NULL,    -- 'severe', 'moderate', 'improved'
    ndvi_diff_mean  FLOAT,                   -- NDVI medio de la zona
    area_km2        FLOAT,                   -- área calculada en km²
    pixel_count     INTEGER,                 -- nº de pixels que forman la zona
    geom            GEOMETRY(MULTIPOLYGON, 4326) NOT NULL
);

-- Índice espacial: CRITICAL para rendimiento en consultas geoespaciales.
-- Sin este índice, una consulta "dame zonas dentro de este bounding box"
-- recorre TODA la tabla. Con el índice GIST, usa un árbol espacial (R-tree)
-- y es órdenes de magnitud más rápida.
CREATE INDEX IF NOT EXISTS idx_degradation_zones_geom
    ON degradation_zones USING GIST (geom);

CREATE INDEX IF NOT EXISTS idx_degradation_zones_run_id
    ON degradation_zones (run_id);