# ─────────────────────────────────────────────
# Hito 1: Extracción y exportación Sentinel-2
# ─────────────────────────────────────────────

import ee
import geemap
import os

def get_sentinel2_collection(aoi, start_date, end_date, max_cloud_cover):
    """
    Retorna una ImageCollection de Sentinel-2 filtrada por zona, fecha y nubes.

    Usamos 'S2_SR_HARMONIZED' (Reflectancia de Superficie) en lugar de 'S2_TOA'
    (brillo en tope de atmósfera) porque SR ya tiene la corrección atmosférica
    aplicada. Esto es crítico al comparar dos años distintos (sin corrección,
    las diferencias de humedad atmosférica entre 2021 y 2024 contaminarían el NDVI).
    'HARMONIZED' además unifica el procesamiento entre las distintas versiones
    del procesador de la ESAm sin esto, comparar 2021 vs 2024 introduciría
    un sesgo instrumental.
    """
    collection = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(aoi)
        .filterDate(start_date, end_date)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", max_cloud_cover))
    )
    
    count = collection.size().getInfo()
    print(f"  Imágenes encontradas ({start_date} → {end_date}): {count}")
    
    if count == 0:
        raise ValueError(
            f"Sin imágenes para el periodo {start_date}-{end_date}. "
            "Prueba a aumentar MAX_CLOUD_COVER en config.py"
        )
    
    return collection


def calculate_ndvi(image):
    """
    Calcula el NDVI para una imagen Sentinel-2 y lo añade como banda nueva.

    NDVI = (NIR - Red) / (NIR + Red)
         = (B8  - B4)  / (B8  + B4)

    En Sentinel-2 SR, los valores de banda van de 0 a 10000 (no 0 a 1).
    La operación de división normaliza el resultado automáticamente al rango [-1, 1],
    así que NO necesitas dividir por 10000 antes, la fórmula lo hace implícitamente.

    .rename('NDVI') es importante: por defecto GEE nombra el resultado 'B8' 
    (el nombre de la primera banda de la operación), lo que causaría confusión.
    """
    ndvi = image.normalizedDifference(["B8", "B4"]).rename("NDVI")
    
    # Añadimos el NDVI como banda extra a la imagen original.
    # Así conservamos las bandas RGB por si se quiere hacer visualizaciones en color.
    return image.addBands(ndvi)


def get_composite_ndvi(aoi, start_date, end_date, max_cloud_cover):
    """
    Pipeline completo: filtra → calcula NDVI por imagen → mediana temporal.
    
    El orden importa:
    1. Primero calculamos NDVI en CADA imagen de la colección (map).
    2. Luego tomamos la MEDIANA de todos esos NDVI imagen a imagen.
    
    ¿Por qué no al revés (mediana primero, NDVI después)?
    Porque la mediana de bandas crudas puede crear valores de pixel que no
    corresponden a ninguna observación real (artifact de compositing). 
    Calcular NDVI primero y luego medianear el NDVI es más correcto físicamente.
    """
    collection = get_sentinel2_collection(aoi, start_date, end_date, max_cloud_cover)
    
    # .map() en GEE = aplicar una función a cada imagen de la colección.
    # Es el equivalente al map() de Python, pero ejecutado en los servidores de Google.
    ndvi_collection = collection.map(calculate_ndvi)
    
    # Imagen compuesta: mediana del NDVI a lo largo del tiempo, recortada al AOI.
    composite = ndvi_collection.median().select("NDVI").clip(aoi)
    
    return composite


def export_ndvi_image(image, filename, aoi, scale):
    """
    Exporta una imagen GEE a un archivo GeoTIFF en disco local.
    
    geemap.ee_export_image() es un wrapper que internamente usa la 
    API de exportación de GEE pero descarga directamente a tu disco,
    sin necesitar Google Drive como intermediario.
    
    El archivo resultante es un GeoTIFF con:
    - 1 banda (NDVI), valores float entre -1 y 1
    - CRS: EPSG:4326 (WGS84, coordenadas geográficas estándar)
    - Resolución: según el parámetro 'scale'
    """
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    
    print(f"Exportando: {filename}  (escala: {scale}m/px) ...")
    geemap.ee_export_image(
        image,
        filename=filename,
        scale=scale,
        region=aoi,
        file_per_band=False,
        crs="EPSG:4326",
    )
    print(f"Guardado: {filename}")
    
    return filename


def calculate_ndvi_difference(image_baseline, image_compare):
    """
    Calcula el cambio de NDVI entre dos periodos.
    
    diferencia = NDVI_2024 - NDVI_2021
    
    Interpretación del resultado:
     > 0  → más vegetación / recuperación (verde)
     ≈ 0  → sin cambio significativo
     < 0  → pérdida de vegetación / sequía (rojo)
    
    Un umbral de -0.1 es estándar en literatura geoespacial para señalar
    pérdida significativa. Lo usaremos en el Hito 2 para generar la máscara
    de "zonas afectadas".
    """
    diff = image_compare.subtract(image_baseline).rename("NDVI_diff")
    return diff