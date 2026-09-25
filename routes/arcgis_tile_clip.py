"""
Clip a cached ArcGIS tile service (a "tiles only" MapServer) to a boundary
and return it as a zipped GeoTIFF.

Tile-only services have no export or ImageServer endpoint, so the tiles are
read through GDAL's WMS driver (TMS mode), which stitches them on the
service's own Web Mercator grid, then masked to the boundary. The result is
the rendered map image (RGBA), not data values.
"""
import io
import logging
import math
import re
import uuid
import zipfile
from typing import Optional
from urllib.parse import urlparse
from xml.sax.saxutils import escape

import pyproj
import rasterio
import rasterio.mask
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from rasterio.io import MemoryFile
from shapely.geometry import mapping, shape
from shapely.ops import transform

router = APIRouter(tags=["Utility"], prefix='/Utility')

WEB_MERCATOR_EXTENT = 20037508.342787
WEB_MERCATOR_LOD0_RESOLUTION = 156543.03392800014
TILE_SIZE = 256

# Only fetch tiles from ArcGIS hosts, so the endpoint can't be pointed at
# arbitrary URLs.
ALLOWED_HOSTS = re.compile(
    r'^(tiles\d*\.arcgis\.com|services\d*\.arcgis\.com|'
    r'tiledbasemaps\.arcgis\.com|server\.arcgisonline\.com)$'
)
TILE_PATH = re.compile(r'/MapServer/tile/\{z\}/\{y\}/\{x\}$')

# Largest output (width * height) we'll build; bigger boundaries get a
# coarser zoom. 8192 x 8192 RGBA is ~256MB in memory.
MAX_PIXELS = 8192 * 8192


class ArcGISTileClipRequest(BaseModel):
    tile_url: str = Field(
        description="Tile URL template ending in /MapServer/tile/{z}/{y}/{x}"
    )
    geojson: dict = Field(description="Polygon/MultiPolygon, Feature or FeatureCollection")
    input_crs: str = Field(default="EPSG:4326", description="CRS of the input GeoJSON")
    min_zoom: int = Field(default=0, ge=0, le=23)
    max_zoom: int = Field(default=19, ge=0, le=23)
    zip_filename: Optional[str] = Field(default=None, description="Name for the output ZIP file")


def extract_geometry(geojson_data):
    """First Polygon/MultiPolygon in the GeoJSON, as a shapely geometry."""
    geojson_type = geojson_data.get("type")
    if geojson_type in ("Polygon", "MultiPolygon"):
        return shape(geojson_data)
    if geojson_type == "Feature":
        return extract_geometry(geojson_data.get("geometry") or {})
    if geojson_type == "FeatureCollection":
        for feature in geojson_data.get("features", []):
            geometry = extract_geometry(feature.get("geometry") or {})
            if geometry is not None:
                return geometry
    return None


def choose_zoom(bounds, min_zoom, max_zoom):
    """Finest zoom whose pixel window over `bounds` fits MAX_PIXELS."""
    minx, miny, maxx, maxy = bounds
    for zoom in range(max_zoom, min_zoom - 1, -1):
        resolution = WEB_MERCATOR_LOD0_RESOLUTION / 2 ** zoom
        width = math.ceil((maxx - minx) / resolution) + 1
        height = math.ceil((maxy - miny) / resolution) + 1
        if width * height <= MAX_PIXELS:
            return zoom
    raise HTTPException(
        status_code=400,
        detail="Boundary is too large to export at this layer's coarsest zoom",
    )


def tms_xml(tile_url, zoom):
    """GDAL WMS (TMS) description of an ArcGIS {z}/{y}/{x} tile cache, with
    `zoom` as the full-resolution level."""
    server_url = tile_url.replace('{z}', '${z}').replace('{y}', '${y}').replace('{x}', '${x}')
    return f"""<GDAL_WMS>
  <Service name="TMS"><ServerUrl>{escape(server_url)}</ServerUrl></Service>
  <DataWindow>
    <UpperLeftX>-{WEB_MERCATOR_EXTENT}</UpperLeftX><UpperLeftY>{WEB_MERCATOR_EXTENT}</UpperLeftY>
    <LowerRightX>{WEB_MERCATOR_EXTENT}</LowerRightX><LowerRightY>-{WEB_MERCATOR_EXTENT}</LowerRightY>
    <TileLevel>{zoom}</TileLevel><TileCountX>1</TileCountX><TileCountY>1</TileCountY>
    <YOrigin>top</YOrigin>
  </DataWindow>
  <Projection>EPSG:3857</Projection>
  <BlockSizeX>{TILE_SIZE}</BlockSizeX><BlockSizeY>{TILE_SIZE}</BlockSizeY>
  <BandsCount>4</BandsCount>
  <ZeroBlockHttpCodes>204,404</ZeroBlockHttpCodes>
  <MaxConnections>8</MaxConnections>
</GDAL_WMS>"""


# A plain `def` so FastAPI runs the blocking GDAL work in its threadpool
# rather than on the event loop.
@router.post("/arcgis_tiles/clip", response_class=Response)
def clip_arcgis_tiles_with_polygon(request: ArcGISTileClipRequest):
    """
    Stitch an ArcGIS tile cache over a GeoJSON polygon/multipolygon, mask it
    to the boundary, and return a zipped, compressed EPSG:3857 GeoTIFF.
    """
    parsed = urlparse(request.tile_url)
    if (
        parsed.scheme != 'https'
        or not ALLOWED_HOSTS.match(parsed.hostname or '')
        or not TILE_PATH.search(parsed.path)
    ):
        raise HTTPException(
            status_code=400,
            detail="tile_url must be an ArcGIS https .../MapServer/tile/{z}/{y}/{x} URL",
        )
    if request.min_zoom > request.max_zoom:
        raise HTTPException(status_code=400, detail="min_zoom must not exceed max_zoom")

    geometry = extract_geometry(request.geojson)
    if geometry is None or geometry.is_empty:
        raise HTTPException(
            status_code=400,
            detail="Could not extract Polygon or MultiPolygon from provided GeoJSON",
        )

    try:
        to_web_mercator = pyproj.Transformer.from_crs(
            request.input_crs, "EPSG:3857", always_xy=True
        ).transform
        geometry = transform(to_web_mercator, geometry)
        zoom = choose_zoom(geometry.bounds, request.min_zoom, request.max_zoom)

        with rasterio.open(tms_xml(request.tile_url, zoom)) as src:
            out_image, out_transform = rasterio.mask.mask(
                src, [mapping(geometry)], crop=True, filled=True, nodata=0
            )
            crs = src.crs

        # Missing tiles read as all-zero blocks, so no alpha anywhere means
        # the service has nothing inside the boundary.
        if not out_image[3].any():
            raise HTTPException(
                status_code=404,
                detail="This layer has no map tiles inside the boundary",
            )

        height, width = out_image.shape[1], out_image.shape[2]
        profile = {
            'driver': 'GTiff',
            'height': height,
            'width': width,
            'count': 4,
            'dtype': out_image.dtype,
            'crs': crs,
            'transform': out_transform,
            'photometric': 'RGB',
            # Marks the 4th band as alpha (transparent outside the boundary).
            'alpha': 'YES',
            'compress': 'deflate',
        }
        if width >= TILE_SIZE and height >= TILE_SIZE:
            profile.update(tiled=True, blockxsize=TILE_SIZE, blockysize=TILE_SIZE)
        with MemoryFile() as memfile:
            with memfile.open(**profile) as dst:
                dst.write(out_image)
            tiff_bytes = memfile.read()

        unique_id = str(uuid.uuid4())[:8]
        zip_filename = request.zip_filename or f"arcgis_tiles_clip_{unique_id}.zip"
        if not zip_filename.lower().endswith('.zip'):
            zip_filename += '.zip'
        tiff_filename = f"{zip_filename[:-4]}.tif"

        memory_file = io.BytesIO()
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(tiff_filename, tiff_bytes)

        return Response(
            content=memory_file.getvalue(),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{zip_filename}"',
                # Lets the frontend report the resolution it got.
                "X-Tile-Zoom": str(zoom),
                "Access-Control-Expose-Headers": "X-Tile-Zoom",
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        logging.exception("Error processing ArcGIS tile clip request")
        raise HTTPException(status_code=500, detail=f"Error processing request: {str(e)}")
