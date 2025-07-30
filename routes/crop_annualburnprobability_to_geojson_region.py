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
import json
from shapely.geometry import shape

def debug_print(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    sys.stdout.write(f"[{timestamp}] {msg}\n")
    sys.stdout.flush()

def get_region_boundary_from_geojson(geojson_str: str):
    """
    Convert GeoJSON string to GeoDataFrame in EPSG:3310 projection

    Parameters:
    -----------
    geojson_str : str
        GeoJSON string representing the region boundary

    Returns:
    --------
    region_data : GeoDataFrame
        Region boundary in EPSG:3310 projection
    """
    debug_print("Parsing GeoJSON string...")
    try:
        geojson_data = json.loads(geojson_str)
        region_data = gpd.GeoDataFrame.from_features(geojson_data['features'], crs="EPSG:4326")
    except Exception as e:
        raise ValueError(f"Failed to parse GeoJSON: {str(e)}")

    if region_data.empty:
        raise ValueError("GeoJSON contains no valid features")

    debug_print(f"Retrieved region_data: {type(region_data)}, shape: {region_data.shape}, columns: {list(region_data.columns)}")
    if not isinstance(region_data, gpd.GeoDataFrame) or 'geometry' not in region_data.columns:
        raise TypeError(f"Expected GeoDataFrame with 'geometry' column, got {type(region_data)} with columns {list(region_data.columns)}")

    debug_print("Reprojecting region geometry to EPSG:3310...")
    region_data_3310 = region_data.to_crs("EPSG:3310")
    debug_print(f"Reprojected region_data_3310: {type(region_data_3310)}, shape: {region_data_3310.shape}, columns: {list(region_data_3310.columns)}")
    return region_data_3310

def get_scatter_points(db: Session, region_gdf: gpd.GeoDataFrame):
    """
    Retrieve points from the database that intersect with the region geometry

    Parameters:
    -----------
    db : Session
        Database session
    region_gdf : GeoDataFrame
        Region geometry in EPSG:4326

    Returns:
    --------
    points_df : DataFrame
        DataFrame with x,y coordinates in EPSG:4326
    """
    debug_print("Retrieving treatment points intersecting with region...")
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
       FROM its.activities_report_20250110 AS its, bbox, region_geom
       WHERE ST_Intersects(its.geom, bbox.geom)
         AND ST_Contains(region_geom.geom, its.geom)
         AND year_txt ~ '^[0-9]+$'
         AND CAST(year_txt AS INTEGER) BETWEEN 2021 AND 2023;
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

def get_wms_image(geoserver_url, layer_name, style_name, min_x, min_y, max_x, max_y, width=800, height=400):
    """
    Fetch WMS layer image from GeoServer in EPSG:3310

    Parameters:
    -----------
    geoserver_url : str
        GeoServer base URL
    layer_name : str
        WMS layer name
    style_name : str
        WMS style name
    min_x, min_y, max_x, max_y : float
        Bounding box in EPSG:3310
    width, height : int
        Image dimensions in pixels

    Returns:
    --------
    numpy.ndarray
        Image array in RGBA format
    """
    wms_url = f"{geoserver_url}/wms"
    try:
        debug_print(f"Connecting to WMS: {wms_url} for layer {layer_name}")
        wms = WebMapService(wms_url, version='1.3.0')
        
        debug_print(f"Requesting WMS image for {layer_name} with style {style_name} in EPSG:3310")
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
        return img_array

    except Exception as e:
        debug_print(f"Error fetching WMS image: {str(e)}")
        raise

def get_wms_legend(geoserver_url, layer_name, style_name):
    """
    Fetch the legend graphic from GeoServer for the specified layer and style

    Parameters:
    -----------
    geoserver_url : str
        GeoServer base URL
    layer_name : str
        WMS layer name
    style_name : str
        WMS style name

    Returns:
    --------
    PIL.Image
        Legend graphic as a PIL Image
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

def create_region_map(db: Session, geojson_str: str = None, region_name: str = None,
                     geoserver_url: str = None, layer_name: str = None, layer_title: str = None,
                     output_png=None, dpi=100, figsize=(16, 4), point_color='blue',
                     point_size=7, point_alpha=0.7, points_df=None, color_by_year=True,
                     region_gdf=None, min_value=None, max_value=None, 
                     min_value_color=None, max_value_color=None, table_name=None, column_name=None):
    """
    Create a map for a region defined by GeoJSON with OpenStreetMap background, WMS layer, and treatment points

    Parameters:
    -----------
    db : Session
        Database session
    geojson_str : str, optional
        GeoJSON string representing the region boundary
    region_name : str, optional
        Name of the region to map
    geoserver_url : str, optional
        GeoServer base URL
    layer_name : str, optional
        WMS layer name
    layer_title : str, optional
        Title for the layer in the map
    output_png : str, optional
        Output path for PNG file
    dpi : int, optional
        DPI for the output image
    figsize : tuple, optional
        Figure size (width, height)
    point_color : str, optional
        Color for points if not coloring by year
    point_size : float, optional
        Size of the scatter points
    point_alpha : float, optional
        Transparency of the scatter points
    points_df : DataFrame, optional
        DataFrame containing points to plot
    color_by_year : bool, optional
        If True, color points by year_txt column; if False, use point_color for all points
    region_gdf : GeoDataFrame, optional
        Precomputed region geometry
    min_value : float, optional
        Deprecated, unused parameter for colormap minimum value
    max_value : float, optional
        Deprecated, unused parameter for colormap maximum value
    min_value_color : str, optional
        Deprecated, unused parameter for minimum value color
    max_value_color : str, optional
        Deprecated, unused parameter for maximum value color
    table_name : str, optional
        Unused parameter for table name (for compatibility)
    column_name : str, optional
        Unused parameter for column name (for compatibility)

    Returns:
    --------
    str
        Base64 encoded PNG image
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
        if geojson_str is None:
            raise ValueError("Either geojson_str or region_gdf must be provided")
        region_data_3310 = get_region_boundary_from_geojson(geojson_str)
        region_data_4326 = region_data_3310.to_crs("EPSG:4326")

    # Validate GeoDataFrame
    if not isinstance(region_data_3310, gpd.GeoDataFrame) or 'geometry' not in region_data_3310.columns:
        raise TypeError(f"region_data_3310 is not a valid GeoDataFrame: type={type(region_data_3310)}, columns={list(region_data_3310.columns)}")
    
    region_geom_3310 = region_data_3310['geometry'].iloc[0] if region_data_3310.shape[0] > 0 else None
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
                points_df = get_scatter_points(db, region_data_4326)
    else:
        points_df = get_scatter_points(db, region_data_4326)

    if points_df.empty:
        return None

    points_df = points_df.rename(columns={'lon': 'x', 'lat': 'y'})

    # Get bounding box in EPSG:3310 for WMS request
    min_x, min_y, max_x, max_y = region_geom_3310.bounds

    # Add a buffer in EPSG:3310
    x_range = max_x - min_x
    y_range = max_y - min_y
    buffer_factor = 0.2
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

    # Add OpenStreetMap basemap
    try:
        debug_print(f"Adding OpenStreetMap basemap with bounds: ({min_lon}, {min_lat}, {max_lon}, {max_lat})")
        ctx.add_basemap(
            ax,
            crs="EPSG:4326",
            source=ctx.providers.OpenStreetMap.Mapnik,
            zoom=10,
            attribution="(C) OpenStreetMap contributors",
            zorder=1
        )
        debug_print("Basemap added successfully")
    except Exception as e:
        debug_print(f"Error adding OpenStreetMap basemap: {str(e)}")
        try:
            debug_print("Attempting fallback basemap provider (Stamen Terrain)...")
            ctx.add_basemap(
                ax,
                crs="EPSG:4326",
                source=ctx.providers.Stamen.Terrain,
                zoom=10,
                attribution="(C) Stamen Design, OpenStreetMap contributors",
                zorder=1
            )
            debug_print("Fallback basemap added successfully")
        except Exception as e2:
            debug_print(f"Error adding fallback basemap: {str(e2)}")
            ax.set_facecolor('lightblue')
            debug_print("Using fallback light blue background")

    # Add WMS layer if layer_name is provided
    if layer_name:
        style_name = f"{layer_name.split(':')[-1]}_std" if ':' in layer_name else f"{layer_name}_std"
        try:
            debug_print(f"Fetching WMS layer: {layer_name} with style: {style_name}")
            wms_image = get_wms_image(geoserver_url, layer_name, style_name, min_x, min_y, max_x, max_y)
            
            # Create a temporary raster for clipping in EPSG:3310
            transform = rasterio.transform.from_bounds(min_x, min_y, max_x, max_y, wms_image.shape[1], wms_image.shape[0])
            debug_print(f"WMS transform in EPSG:3310: {transform}")
            profile = {
                'driver': 'PNG',
                'height': wms_image.shape[0],
                'width': wms_image.shape[1],
                'count': 4,
                'dtype': np.uint8,
                'crs': 'EPSG:3310',
                'transform': transform,
                'nodata': None
            }

            with MemoryFile() as memfile:
                with memfile.open(**profile) as dst:
                    dst.write(wms_image.transpose(2, 0, 1))
                with memfile.open() as src:
                    # Clip to region polygon in EPSG:3310
                    out_image, out_transform = rasterio.mask.mask(
                        src,
                        [region_geom_3310],
                        crop=True,
                        all_touched=True,
                        filled=False
                    )
                    debug_print(f"Clipped image shape: {out_image.shape}, transform: {out_transform}")

                    out_meta = src.meta.copy()
                    out_meta.update({
                        'height': out_image.shape[1],
                        'width': out_image.shape[2],
                        'transform': out_transform
                    })

                    # Reproject to EPSG:4326
                    dst_crs = 'EPSG:4326'
                    transform_4326, width_4326, height_4326 = calculate_default_transform(
                        src.crs, dst_crs, out_image.shape[2], out_image.shape[1],
                        *rasterio.transform.array_bounds(out_image.shape[1], out_image.shape[2], out_transform)
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
                            resampling=Resampling.nearest
                        )
                    debug_print(f"Reprojected image shape: {dst_array.shape}, transform: {transform_4326}")

                    # Plot the reprojected WMS image
                    left = transform_4326[2]
                    right = left + width_4326 * transform_4326[0]
                    bottom = transform_4326[5] + height_4326 * transform_4326[4]
                    top = transform_4326[5]
                    debug_print(f"Plotting WMS image with extent: ({left}, {right}, {bottom}, {top})")

                    # Apply polygon clip in matplotlib
                    coords = []
                    for geom in region_data_4326['geometry']:
                        if geom.geom_type == 'Polygon':
                            coords.extend(list(geom.exterior.coords))
                        elif geom.geom_type == 'MultiPolygon':
                            for part in geom.geoms:
                                coords.extend(list(part.exterior.coords))
                    path = Path(coords)
                    patch = PathPatch(path, facecolor='none', edgecolor='none')
                    ax.add_patch(patch)

                    im = ax.imshow(
                        dst_array.transpose(1, 2, 0),
                        extent=[left, right, bottom, top],
                        alpha=0.6,
                        zorder=2
                    )
                    im.set_clip_path(patch)

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
        year_colors = {'2021': 'green', '2022': 'purple', '2023': 'blue'}
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