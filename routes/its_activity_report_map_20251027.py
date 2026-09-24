import rasterio
import rasterio.mask
import geopandas as gpd
import requests
import numpy as np
import pandas as pd
import io
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy import text
import sys
from datetime import datetime
import contextily as ctx
from matplotlib.path import Path
from matplotlib.patches import PathPatch
import base64
from owslib.wms import WebMapService
from rasterio.io import MemoryFile
from rasterio.warp import calculate_default_transform, reproject, Resampling
from PIL import Image
from matplotlib.offsetbox import AnchoredOffsetbox, OffsetImage, AnnotationBbox
import warnings
from shapely.validation import make_valid
from shapely.geometry import MultiPolygon, Polygon
from rasterio.merge import merge
import os
import tempfile

def debug_print(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    sys.stdout.write(f"[{timestamp}] {msg}\n")
    sys.stdout.flush()

def remove_duplicate_vertices(coords):
    """Remove consecutive duplicate vertices from a coordinate list."""
    if not coords:
        return coords
    unique_coords = [coords[0]]
    for coord in coords[1:]:
        if coord != unique_coords[-1]:
            unique_coords.append(coord)
    if unique_coords[0] != unique_coords[-1]:
        unique_coords.append(unique_coords[0])
    return unique_coords

def get_region_boundary(db: Session, table_name: str, column_name: str, region_name: str):
    """
    Retrieve the boundary for a specified region from the database
    """
    debug_print(f"Retrieving {region_name} boundary from {table_name}.{column_name}...")

    query = f"""
    SELECT {column_name} as name, geom, ST_NPoints(geom) as vertex_count
    FROM {table_name}
    WHERE {column_name} = :region_name
    """

    with db.bind.connect() as conn:
        region_data = gpd.read_postgis(text(query), conn,
                                      geom_col='geom',
                                      params={"region_name": region_name})

    if region_data.empty:
        raise ValueError(f"Region '{region_name}' not found in {table_name}.{column_name}")

    debug_print(f"Retrieved region_data: {type(region_data)}, shape: {region_data.shape}, columns: {list(region_data.columns)}")
    debug_print(f"Total geometry vertex count: {region_data['vertex_count'].iloc[0]}")

    # Check MultiPolygon parts and interior rings
    geom = region_data['geom'].iloc[0]
    if isinstance(geom, MultiPolygon):
        debug_print(f"Geometry is MultiPolygon with {len(geom.geoms)} parts")
        for i, part in enumerate(geom.geoms):
            exterior_vertices = len(part.exterior.coords)
            interior_count = len(part.interiors)
            interior_vertices = sum(len(ring.coords) for ring in part.interiors)
            area = part.area
            debug_print(f"Part {i+1}: {exterior_vertices} exterior vertices, {interior_count} interior rings, {interior_vertices} interior vertices, area: {area:.2f} sq units")
            if exterior_vertices < 50:
                debug_print(f"Warning: Part {i+1} is small (vertices={exterior_vertices})")
    else:
        exterior_vertices = len(geom.exterior.coords)
        interior_count = len(geom.interiors)
        interior_vertices = sum(len(ring.coords) for ring in geom.interiors)
        area = geom.area
        debug_print(f"Geometry is Polygon: {exterior_vertices} exterior vertices, {interior_count} interior rings, {interior_vertices} interior vertices, area: {area:.2f} sq units")
        if exterior_vertices < 50:
            debug_print(f"Warning: Polygon is small (vertices={exterior_vertices})")

    # Validate geometry without simplification
    debug_print("Validating region geometry...")
    region_data['geom'] = region_data['geom'].apply(lambda geom: make_valid(geom) if not geom.is_valid else geom)

    debug_print("Reprojecting region geometry to EPSG:3310...")
    if region_data.crs is None:
        region_data.crs = "EPSG:4326"

    region_data_3310 = region_data.to_crs("EPSG:3310")
    debug_print(f"Reprojected region_data_3310: {type(region_data_3310)}, shape: {region_data_3310.shape}, columns: {list(region_data_3310.columns)}")
    return region_data_3310

def get_scatter_points(db: Session, table_name, column_name, region_name, region_gdf=None):
    """
    Retrieve points from the database that intersect with the region geometry
    """
    debug_print("Retrieving treatment points intersecting with region...")
    if region_gdf is not None:
        region_wkt = region_gdf.geometry.unary_union.wkt
        query = f"""
           WITH region_geom AS (
                SELECT ST_SetSRID(ST_GeomFromText('{region_wkt}'), 4269) AS geom
           ),
           bbox AS (
                SELECT ST_SetSRID(ST_Extent(geom), 4269)::geometry AS geom
                FROM region_geom
           )
           SELECT
               ST_X(its.geom) AS lon,
               ST_Y(its.geom) AS lat,
               its.activity_quantity,
               its.year_txt
           FROM its.activity_report20251027 AS its, bbox, region_geom
           WHERE ST_Intersects(its.geom, bbox.geom)
             AND ST_Contains(region_geom.geom, its.geom)
             AND year_txt ~ '^[0-9]+$'
             AND CAST(year_txt AS INTEGER) BETWEEN 2021 AND 2024;
        """
    else:
        query = f"""
           WITH region_geom AS (
                   SELECT ST_Transform(geom, 4269) AS geom
                   FROM {table_name}
                   WHERE {column_name} = '{region_name}'
                ),
                bbox AS (
                     SELECT ST_SetSRID(ST_Extent(geom), 4269)::geometry AS geom
                     FROM region_geom
                )
            SELECT
                ST_X(its.geom) AS lon,
                ST_Y(its.geom) AS lat,
                its.activity_quantity,
                its.year_txt
            FROM its.activity_report20251027 AS its, bbox, region_geom
            WHERE ST_Intersects(its.geom, bbox.geom)
              AND ST_Contains(region_geom.geom, its.geom)
              AND year_txt ~ '^[0-9]+$'
              AND CAST(year_txt AS INTEGER) BETWEEN 2021 AND 2024;
        """
    debug_print(query)

    with db.bind.connect() as conn:
        points_df = pd.read_sql_query(text(query), conn)

    if points_df.empty:
        print("No treatment points found in this region")
    else:
        print(f"Found {len(points_df)} points to plot")

    debug_print(f"points_df: {points_df.shape}")
    return points_df

def get_wms_image(geoserver_url, layer_name, style_name, min_x, min_y, max_x, max_y, width=3200, height=1600):
    """
    Fetch WMS layer image from GeoServer in EPSG:3310
    """
    wms_url = "https://sparcal.sdsc.edu/geoserver/rrk/wms"
    try:
        debug_print(f"Connecting to WMS: {wms_url} for layer {layer_name}")
        wms = WebMapService(wms_url, version='1.3.0')
        
        debug_print(f"Requesting WMS image for {layer_name} with style {style_name} in EPSG:3310, size {width}x{height}")
        response = wms.getmap(
            layers=[layer_name],
            styles=[style_name],
            srs='EPSG:3310',
            bbox=(min_x, min_y, max_x, max_y),
            size=(width, height),
            format='image/png',
            transparent=True
        )

        content_type = response.info().get('content-type')
        debug_print(f"WMS response content-type: {content_type}")
        if content_type != 'image/png':
            debug_print(f"WMS response headers: {response.info()}")
            try:
                debug_print(f"WMS response content: {response.read().decode('utf-8')[:500]}")
            except Exception as e:
                debug_print(f"Could not decode WMS response content: {str(e)}")
            raise Exception(f"Unexpected content type: {content_type}")

        img_data = io.BytesIO(response.read())
        img = Image.open(img_data)
        img = img.convert('RGBA')
        img_array = np.array(img)
        debug_print(f"WMS image shape: {img_array.shape}, bounds: ({min_x}, {min_y}, {max_x}, {max_y})")
        
        # Check for non-transparent pixels
        alpha_channel = img_array[:, :, 3]
        non_transparent_pixels = np.sum(alpha_channel > 0)
        debug_print(f"Non-transparent pixels in WMS image: {non_transparent_pixels}")
        if non_transparent_pixels == 0:
            debug_print("Warning: WMS image contains no non-transparent pixels")
        
        return img_array

    except Exception as e:
        debug_print(f"Error fetching WMS image: {str(e)}")
        raise

def get_wms_legend(geoserver_url, layer_name, style_name):
    """
    Fetch the legend graphic from GeoServer for the specified layer and style
    """
    legend_url = f"{geoserver_url}/wms?REQUEST=GetLegendGraphic&VERSION=1.3.0&FORMAT=image/png&LAYER={layer_name}&STYLE={style_name}&legend_options=fontAntiAliasing%3Atrue%3BfontSize%3A10%3BfontName%3AArial%3Bdx%3A5%3BabsoluteMargins%3Atrue"
    debug_print(f"Fetching legend graphic from: {legend_url}")
    try:
        response = requests.get(legend_url)
        if response.status_code != 200:
            debug_print(f"Failed to fetch legend: status code {response.status_code}")
            raise Exception(f"Failed to fetch legend graphic: {response.text[:500]}")
        if response.headers.get('content-type') != 'image/png':
            debug_print(f"Unexpected legend content type: {response.headers.get('content-type')}")
            raise Exception(f"Unexpected legend content type: {response.headers.get('content-type')}")
        img_data = io.BytesIO(response.content)
        img = Image.open(img_data)
        img = img.convert('RGBA')
        debug_print("Legend graphic fetched successfully")
        return img
    except Exception as e:
        debug_print(f"Error fetching legend graphic: {str(e)}")
        return None

def create_region_map(db: Session, table_name: str, column_name: str, region_name: str,
                     geoserver_url: str, layer_name: str, layer_title: str,
                     output_png=None, dpi=150, figsize=(16, 5), point_color='blue',
                     point_size=7, point_alpha=0.7, points_df=None, color_by_year=True, 
                     region_gdf=None, min_value=None, max_value=None, 
                     min_value_color=None, max_value_color=None,
                     apply_matplotlib_clip=True, simplify_geometry=False, clip_individual_polygons=True):
    """
    Create a map for a custom region with MapTiler Positron background, WMS layer, and treatment points
    """
    # Warn about deprecated parameters
    if any(param is not None for param in [min_value, max_value, min_value_color, max_value_color]):
        warnings.warn("Parameters min_value, max_value, min_value_color, and max_value_color are deprecated and unused in create_region_map.", DeprecationWarning)
        debug_print("Warning: Deprecated parameters min_value, max_value, min_value_color, or max_value_color provided.")

    # Override geoserver_url to ensure correct WMS endpoint
    geoserver_url = "https://sparcal.sdsc.edu/geoserver/rrk"

    # Define legend units dictionary
    legend_units = {
        "cstocks_turnovertime_202009_202312_t1_v5": "Years",
        "waterflux_aetfrac_202109_202312_t1_v5": "AET in mm/P in mm",
        "sb535tribalboundaries_202205_202312_t1_v5": "",
        "annualburnprobability_202212_202406_t1_v5": "Probability",
        "aquaticspecrichness_201802_202209_t1_v5": "Count",
        "bandtailedpigeonsuithab_202304_202406_t1_v5": "",
        "canopycover_202006_202312_t1_v5": "Percent",
        "canopylayercount_202006_202312_t1_v5": "Count",
        "canopyvegheight_202006_202312_t1_v5": "Meters",
        "cumulshrubcoverloss_19912020_202312_t1_v5": "Absolute Cover Loss",
        "cumultreecoverloss_19912020_202312_t1_v5": "Absolute Cover Loss",
        "damagepotential_202212_202406_t1_v5": "Description",
        "earlyseralstagedist_202304_202406_t1_v5": "Proportion to HUC12",
        "emberloadindex_202212_202406_t1_v5": "Relative # of Embers",
        "frid_conditionclass_2022_202401_t1_v5": "Percent Departure",
        "frid_meanprct_19082022_202401_t1_v5": "Percent",
        "frid_meanprct_19702022_202401_t1_v5": "Percent",
        "frid_timesincelastfire_2022_202401_t1_v5": "Years",
        "forestraptorspecrichness_202304_202406_t1_v5": "Count",
        "cavitynestersspecrichness_202304_202406_t1_v5": "Count",
        "herbivoresspecrichness_202304_202406_t1_v5": "Count",
        "insectivoresspecrichness_202304_202406_t1_v5": "Count",
        "predatorsspecrichness_202304_202406_t1_v5": "Count",
        "seedsporedispspecrichness_202304_202406_t1_v5": "Count",
        "soilaeratorsspecrichness_202304_202406_t1_v5": "Count",
        "herbcoverratio_202112_202312_t1_v5": "Absolute Cover",
        "housingburdenprct_2020_202209_t1_v5": "Percent",
        "hummingbirdspecrichness_202304_202406_t1_v5": "Count",
        "wldfireignallcauses_19922020_202312_t1_v5": "",
        "wldfireigncausehuman_19922020_202312_t1_v5": "",
        "lateseralstagedistr_202304_202406_t1_v5": "Proportion in HUC12",
        "loggerheadshrikesuithab_202304_202406_t1_v5": "Suitability",
        "mountainlionsuithab_202304_202406_t1_v5": "Suitability",
        "nuttallswoodpeckersuithab_202304_202406_t1_v5": "Suitability",
        "openrangeraptorspecrichness_202304_202406_t1_v5": "Count",
        "prctimpervioussurface_2019_202312_t1_v5": "Percent",
        "potentialavoidedsmoke_202209_202312_t1_v5": "Value",
        "potentialtotalsmoke_202209_202312_t1_v5": "Value",
        "povertyprct_2020_202209_t1_v5": "Percent",
        "waterflux_runoff_202109_202312_t1_v5": "mm/yr",
        "presentdayconn_202301_202401_t1_v5": "",
        "probfireseverityhigh_202208_202406_t1_v5": "> 8ft Flame Lengths",
        "probfireseveritylow_202208_202406_t1_v5": "< 4ft Flame Lengths",
        "probfireseveritymod_202208_202406_t1_v5": "4-8ft Flame Lengths",
        "ringtailcatsuithab_202304_202406_t1_v5": "Suitability",
        "riparianhab_201904_202209_t1_v5": "Presence",
        "risktreedieoff_202112_202312_t1_v5": "Value",
        "seralstagedist_202304_202406_t1_v5": "Stage of Secondary Successional Development",
        "shrubcoverratio_202112_202312_t1_v5": "Absolute Cover",
        "sourceemberloadtobldgs_202212_202406_t1_v5": "Relative Index",
        "cstockstotalabove_202009_202312_t1_v5": "Grams Dry matter/m2",
        "treecoverratio_202112_202312_t1_v5": "Absolute Cover",
        "unemploymentprct_2020_202209_t1_v5": "Percent",
        "wildfirehazardpotential_202112_202406_t1_v5": "Type",
        "wildlifespecrichness_202304_202406_t1_v5": "Count"
    }

    # Get region boundary
    if region_gdf is not None:
        debug_print("Using provided region_gdf")
        region_data_3310 = region_gdf.to_crs("EPSG:3310")
        region_data_4326 = region_gdf.to_crs("EPSG:4326")
    else:
        region_data_3310 = get_region_boundary(db, table_name, column_name, region_name)
        region_data_4326 = region_data_3310.to_crs("EPSG:4326")

    # Validate GeoDataFrame
    if not isinstance(region_data_3310, gpd.GeoDataFrame) or 'geom' not in region_data_3310.columns:
        raise TypeError(f"region_data_3310 is not a valid GeoDataFrame: type={type(region_data_3310)}, columns={list(region_data_3310.columns)}")
    
    region_geom_3310 = region_data_3310['geom'].iloc[0] if region_data_3310.shape[0] > 0 else None
    if region_geom_3310 is None:
        raise ValueError("No geometry found in region_data_3310")
    
    debug_print(f"Region bounds in EPSG:3310: {region_geom_3310.bounds}")
    debug_print(f"Region bounds in EPSG:4326: {region_data_4326.total_bounds}")

    # Get treatment points
    if points_df is not None:
        if points_df.empty:
            return None
        if 'x' not in points_df.columns or 'y' not in points_df.columns:
            if 'lon' in points_df.columns and 'lat' in points_df.columns:
                points_df = points_df.rename(columns={'lon': 'x', 'lat': 'y'})
            else:
                points_df = get_scatter_points(db, table_name, column_name, region_name, region_gdf)
    else:
        points_df = get_scatter_points(db, table_name, column_name, region_name, region_gdf)

    if points_df.empty:
        return None

    points_df = points_df.rename(columns={'lon': 'x', 'lat': 'y'})

    # Get bounding box in EPSG:3310 for WMS request
    min_x, min_y, max_x, max_y = region_geom_3310.bounds

    # Increase buffer factor
    x_range = max_x - min_x
    y_range = max_y - min_y
    buffer_factor = 0.4
    min_x -= x_range * buffer_factor
    max_x += x_range * buffer_factor
    min_y -= y_range * buffer_factor
    max_y += y_range * buffer_factor
    debug_print(f"Buffered bounds in EPSG:3310: ({min_x}, {min_y}, {max_x}, {max_y})")

    # Get bounding box in EPSG:4326 for plotting
    region_bounds_4326 = region_data_4326.total_bounds
    min_lon, min_lat, max_lon, max_lat = region_bounds_4326
    lon_range = max_lon - min_lon
    lat_range = max_lat - min_lat
    min_lon -= lon_range * buffer_factor
    max_lon += lon_range * buffer_factor
    min_lat -= lat_range * buffer_factor
    max_lat += lat_range * buffer_factor
    debug_print(f"Buffered bounds in EPSG:4326: ({min_lon}, {min_lat}, {max_lon}, {max_lat})")

    # Ensure aspect ratio is maintained
    fig_aspect = figsize[0] / figsize[1]
    map_aspect = lon_range / lat_range
    if map_aspect > fig_aspect:
        center_lat = (min_lat + max_lat) / 2
        adjusted_lat_range = lon_range / fig_aspect
        min_lat = center_lat - adjusted_lat_range / 2
        max_lat = center_lat + adjusted_lat_range / 2
    else:
        center_lon = (min_lon + max_lon) / 2
        adjusted_lon_range = lat_range * fig_aspect
        min_lon = center_lon - adjusted_lon_range / 2
        max_lon = center_lon + adjusted_lon_range / 2

    # Create figure and axis
    fig, ax = plt.subplots(figsize=figsize)
    ax.set_xlim(min_lon, max_lon)
    ax.set_ylim(min_lat, max_lat)

    # Add MapTiler Positron basemap
    try:
        debug_print(f"Adding MapTiler Positron basemap with bounds: ({min_lon}, {min_lat}, {max_lon}, {max_lat})")
        ctx.add_basemap(
            ax,
            crs="EPSG:4326",
            source='https://api.maptiler.com/maps/positron/{z}/{x}/{y}.png?key=' + os.environ.get('MAPTILER_API_KEY', ''),
            zoom=10,
            attribution="© OpenMapTiles © OpenStreetMap contributors",
            zorder=1
        )
        debug_print("MapTiler Positron basemap added successfully")
    except Exception as e:
        debug_print(f"Error adding MapTiler Positron basemap: {str(e)}")
        try:
            debug_print("Attempting fallback basemap provider (OpenStreetMap)...")
            ctx.add_basemap(
                ax,
                crs="EPSG:4326",
                source=ctx.providers.OpenStreetMap.Mapnik,
                zoom=10,
                attribution="(C) OpenStreetMap contributors",
                zorder=1
            )
            debug_print("Fallback basemap added successfully")
        except Exception as e2:
            debug_print(f"Error adding fallback basemap: {str(e2)}")
            ax.set_facecolor('lightblue')
            debug_print("Using fallback light blue background")

    # Add WMS layer
    if layer_name:
        style_name = f"{layer_name.split(':')[-1]}_std" if ':' in layer_name else f"{layer_name}_std"
    else:
        style_name = ""

    try:
        debug_print(f"Fetching WMS layer: {layer_name} with style: {style_name}")
        wms_image = get_wms_image(geoserver_url, layer_name, style_name, min_x, min_y, max_x, max_y)
        
        # Save unclipped image
        transform = rasterio.transform.from_bounds(min_x, min_y, max_x, max_y, wms_image.shape[1], wms_image.shape[0])
        debug_print(f"WMS transform in EPSG:3310: {transform}")
        profile = {
            'driver': 'GTiff',
            'height': wms_image.shape[0],
            'width': wms_image.shape[1],
            'count': 4,
            'dtype': np.uint8,
            'crs': 'EPSG:3310',
            'transform': transform,
            'nodata': 0
        }
        with rasterio.open('unclipped_wms.tif', 'w', **profile) as dst:
            dst.write(wms_image.transpose(2, 0, 1))
        debug_print("Saved unclipped WMS image to unclipped_wms.tif")

        with MemoryFile() as memfile:
            with memfile.open(**profile) as src:
                src.write(wms_image.transpose(2, 0, 1))
                # Prepare geometry for clipping
                valid_geom_3310 = make_valid(region_geom_3310) if not region_geom_3310.is_valid else region_geom_3310
                if simplify_geometry:
                    valid_geom_3310 = valid_geom_3310.simplify(tolerance=0.5, preserve_topology=True)
                    debug_print("Applied geometry simplification with tolerance=0.5")
                
                # Determine clipping geometries
                if clip_individual_polygons and isinstance(valid_geom_3310, MultiPolygon):
                    clip_geometries = []
                    for part in valid_geom_3310.geoms:
                        if len(part.exterior.coords) >= 50:
                            clip_geometries.append(part)
                        else:
                            debug_print(f"Skipping tiny polygon with {len(part.exterior.coords)} vertices")
                    debug_print(f"Clipping {len(clip_geometries)} individual polygons separately")
                else:
                    clip_geometries = [valid_geom_3310]
                    debug_print("Clipping as a single geometry")

                # Initialize lists for combining clipped images
                clipped_files = []
                pixel_counts = []

                for i, clip_geom in enumerate(clip_geometries):
                    buffer_size = 20.0
                    buffered_geom = clip_geom.buffer(buffer_size)
                    debug_print(f"Clipping geometry {i+1}, bounds: {buffered_geom.bounds}, buffer: {buffer_size}m")
                    
                    # Clip to region polygon in EPSG:3310
                    try:
                        clipped_image, clipped_transform = rasterio.mask.mask(
                            src,
                            [buffered_geom],
                            crop=True,
                            all_touched=True,
                            filled=False,
                            nodata=0,
                            indexes=None
                        )
                        debug_print(f"Clipped image {i+1} shape: {clipped_image.shape}, transform: {clipped_transform}")

                        # Check clipped image for data
                        alpha_channel = clipped_image[3, :, :]
                        non_transparent_pixels = np.sum(alpha_channel > 0)
                        debug_print(f"Non-transparent pixels in clipped image {i+1}: {non_transparent_pixels}")
                        if non_transparent_pixels == 0:
                            debug_print(f"Warning: Clipped WMS image {i+1} contains no non-transparent pixels")
                            continue
                        
                        # Save individual clipped image to temporary file
                        clipped_meta = src.meta.copy()
                        clipped_meta.update({
                            'driver': 'GTiff',
                            'height': clipped_image.shape[1],
                            'width': clipped_image.shape[2],
                            'transform': clipped_transform,
                            'nodata': 0
                        })
                        temp_file = tempfile.NamedTemporaryFile(suffix='.tif', delete=False).name
                        with rasterio.open(temp_file, 'w', **clipped_meta) as clipped_dst:
                            clipped_dst.write(clipped_image)
                        clipped_files.append(temp_file)
                        pixel_counts.append(non_transparent_pixels)
                        debug_print(f"Saved clipped WMS image {i+1} to {temp_file}")
                    except Exception as e:
                        debug_print(f"Error clipping geometry {i+1}: {str(e)}")
                        continue

                # Combine all clipped images
                if clipped_files:
                    clipped_datasets = [rasterio.open(f) for f in clipped_files]
                    try:
                        out_image, out_transform = merge(clipped_datasets, nodata=0)
                        debug_print(f"Combined clipped image shape: {out_image.shape}, transform: {out_transform}")
                        out_meta = clipped_datasets[0].meta.copy()
                        out_meta.update({
                            'height': out_image.shape[1],
                            'width': out_image.shape[2],
                            'transform': out_transform,
                            'nodata': 0
                        })
                        # Check combined image for data
                        alpha_channel = out_image[3, :, :]
                        non_transparent_pixels = np.sum(alpha_channel > 0)
                        debug_print(f"Non-transparent pixels in combined image: {non_transparent_pixels}")
                        if non_transparent_pixels == 0:
                            debug_print("Warning: Combined WMS image contains no non-transparent pixels")
                    finally:
                        # Close datasets and remove temporary files
                        for ds in clipped_datasets:
                            ds.close()
                        for f in clipped_files:
                            try:
                                os.remove(f)
                                debug_print(f"Removed temporary file {f}")
                            except Exception as e:
                                debug_print(f"Error removing temporary file {f}: {str(e)}")
                else:
                    debug_print("No valid clipped images to combine")
                    raise ValueError("No valid clipped images produced")

                # Save combined clipped image
                with rasterio.open('clipped_wms.tif', 'w', **out_meta) as dst:
                    dst.write(out_image)
                debug_print("Saved combined clipped WMS image to clipped_wms.tif")

                # Reproject to EPSG:4326 with high precision
                dst_crs = 'EPSG:4326'
                transform_4326, width_4326, height_4326 = calculate_default_transform(
                    src.crs, dst_crs, out_image.shape[2], out_image.shape[1],
                    *rasterio.transform.array_bounds(out_image.shape[1], out_image.shape[2], out_transform),
                    dst_resolution=(0.00005, 0.00005)
                )
                dst_array = np.zeros((4, height_4326, width_4326), dtype=out_image.dtype)
                for i in range(4):
                    reproject(
                        source=out_image[i],
                        destination=dst_array[i],
                        src_transform=out_transform,
                        src_crs=src.crs,
                        dst_transform=transform_4326,
                        dst_crs=dst_crs,
                        resampling=Resampling.bilinear
                    )
                debug_print(f"Reprojected image shape: {dst_array.shape}, transform: {transform_4326}")

                # Check reprojected image for data
                alpha_channel = dst_array[3, :, :]
                non_transparent_pixels = np.sum(alpha_channel > 0)
                debug_print(f"Non-transparent pixels in reprojected image: {non_transparent_pixels}")
                if non_transparent_pixels == 0:
                    debug_print("Warning: Reprojected WMS image contains no non-transparent pixels")

                # Plot the reprojected WMS image
                left = transform_4326[2]
                right = left + width_4326 * transform_4326[0]
                bottom = transform_4326[5] + height_4326 * transform_4326[4]
                top = transform_4326[5]
                debug_print(f"Plotting WMS image with extent: ({left}, {right}, {bottom}, {top})")

                # Apply polygon clip in matplotlib with explicit hole handling
                if apply_matplotlib_clip:
                    path_commands = []
                    path_vertices = []
                    for geom in region_data_4326['geom']:
                        valid_geom = make_valid(geom) if not geom.is_valid else geom
                        if valid_geom.geom_type == 'Polygon':
                            # Exterior ring
                            exterior = remove_duplicate_vertices([(x, y) for x, y in valid_geom.exterior.coords])
                            if len(exterior) >= 4:
                                path_vertices.extend(exterior)
                                path_commands.extend([Path.MOVETO] + [Path.LINETO] * (len(exterior) - 2) + [Path.CLOSEPOLY])
                            else:
                                debug_print(f"Skipping invalid polygon with {len(exterior)} vertices")
                            # Interior rings (holes)
                            for interior in valid_geom.interiors:
                                interior_coords = remove_duplicate_vertices([(x, y) for x, y in interior.coords])
                                if len(interior_coords) >= 4:
                                    path_vertices.extend(interior_coords)
                                    path_commands.extend([Path.MOVETO] + [Path.LINETO] * (len(interior_coords) - 2) + [Path.CLOSEPOLY])
                                else:
                                    debug_print(f"Skipping invalid interior ring with {len(interior_coords)} vertices")
                        elif valid_geom.geom_type == 'MultiPolygon':
                            for part in valid_geom.geoms:
                                if len(part.exterior.coords) >= 50:
                                    exterior = remove_duplicate_vertices([(x, y) for x, y in part.exterior.coords])
                                    if len(exterior) >= 4:
                                        path_vertices.extend(exterior)
                                        path_commands.extend([Path.MOVETO] + [Path.LINETO] * (len(exterior) - 2) + [Path.CLOSEPOLY])
                                    else:
                                        debug_print(f"Skipping invalid polygon with {len(exterior)} vertices")
                                    for interior in part.interiors:
                                        interior_coords = remove_duplicate_vertices([(x, y) for x, y in interior.coords])
                                        if len(interior_coords) >= 4:
                                            path_vertices.extend(interior_coords)
                                            path_commands.extend([Path.MOVETO] + [Path.LINETO] * (len(interior_coords) - 2) + [Path.CLOSEPOLY])
                                        else:
                                            debug_print(f"Skipping invalid interior ring with {len(interior_coords)} vertices")
                                else:
                                    debug_print(f"Skipping tiny polygon in PathPatch with {len(part.exterior.coords)} vertices")
                    if path_vertices and len(path_vertices) == len(path_commands):
                        path = Path(path_vertices, path_commands, readonly=True)
                        patch = PathPatch(path, facecolor='none', edgecolor='none')
                        ax.add_patch(patch)
                        debug_print("Applying matplotlib PathPatch clipping with explicit hole handling")
                    else:
                        debug_print(f"Error: Path vertices ({len(path_vertices)}) and commands ({len(path_commands)}) length mismatch")
                        apply_matplotlib_clip = False
                else:
                    debug_print("Skipping matplotlib PathPatch clipping for debugging")

                if out_image is not None:
                    im = ax.imshow(
                        dst_array.transpose(1, 2, 0),
                        extent=[left, right, bottom, top],
                        alpha=0.6,
                        zorder=2
                    )
                    if apply_matplotlib_clip and path_vertices and len(path_vertices) == len(path_commands):
                        im.set_clip_path(patch)
                    debug_print("WMS image clipped and plotted successfully")

        # Add GeoServer legend graphic with title
        legend_img = get_wms_legend(geoserver_url, layer_name, style_name)
        if legend_img is not None:
            # Get legend unit based on layer_name
            layer_id = layer_name.split(':')[-1] if ':' in layer_name else layer_name
            legend_title = legend_units.get(layer_id, "")
            debug_print(f"Adding legend for layer {layer_name} with title: {legend_title}")

            # Add legend image to the main plot
            imagebox = OffsetImage(legend_img, zoom=0.6)
            legend_ab = AnnotationBbox(
                imagebox,
                (0.98, 0.15),
                xycoords='axes fraction',
                frameon=True,
                pad=0.25,
                box_alignment=(1, 0)
            )
            ax.add_artist(legend_ab)
            
            # Add title separately below the legend if it exists
            if legend_title:
                ax.text(
                    0.98, 0.08,
                    legend_title,
                    fontsize=9,
                    ha='right',
                    va='bottom',
                    transform=ax.transAxes,
                    style='italic',
                    color='#333333',
                    bbox=dict(facecolor='white', alpha=0.8, edgecolor='none', pad=2)
                )
            
            debug_print("GeoServer legend with separate title added to map")

    except Exception as e:
        debug_print(f"Error adding WMS layer or legend: {str(e)}")
        # Continue without WMS layer or legend but log the error

    # Plot the region boundary
    region_data_4326.plot(ax=ax, facecolor='none', edgecolor='black', linewidth=0.5, zorder=3)

    # Plot scatter points
    if color_by_year and 'year_txt' in points_df.columns:
        year_colors = {'2021': 'green', '2022': 'purple', '2023': 'blue', '2024': 'red'}
        unique_years = sorted([y for y in points_df['year_txt'].unique() if y in year_colors])

        for year in unique_years:
            year_points = points_df[points_df['year_txt'] == year]
            if not year_points.empty:
                ax.scatter(
                    year_points['x'],
                    year_points['y'],
                    color=year_colors[year],
                    s=point_size,
                    alpha=point_alpha,
                    label=f'{year} Activities',
                    marker='o',
                    edgecolor='white',
                    linewidth=0.2,
                    zorder=4
                )
    else:
        ax.scatter(
            points_df['x'],
            points_df['y'],
            color=point_color,
            s=point_size,
            alpha=point_alpha,
            label='Activities',
            marker='o',
            edgecolor='white',
            linewidth=0.2,
            zorder=4
        )

    # Add legend for scatter points
    ax.legend(loc='upper right', fontsize=8, frameon=True, edgecolor='black')
    # ax.set_title(f'{region_name} - {layer_title}', fontsize=12, pad=10)

    # Remove axis ticks
    ax.set_xticks([])
    ax.set_yticks([])

    # Adjust layout
    plt.tight_layout()

    # Save to BytesIO and encode as base64
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=dpi, bbox_inches='tight')
    buf.seek(0)
    img_str = base64.b64encode(buf.read()).decode('utf-8')
    buf.close()
    plt.close(fig)

    if output_png:
        with open(output_png, 'wb') as f:
            f.write(base64.b64decode(img_str))

    return img_str