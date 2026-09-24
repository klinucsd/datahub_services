from fastapi import APIRouter, Query, HTTPException, Response
import matplotlib.pyplot as plt
from owslib.wms import WebMapService
from PIL import Image
import contextily as ctx
from pyproj import Transformer
from io import BytesIO
import logging
import traceback


router = APIRouter(tags=["Utility"], prefix='/Utility')  

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@router.get("/wms_thumbnail")
async def get_wms_thumbnail(
    wms_url: str = Query(..., description="URL to the WMS service"),
    layer_name: str = Query(..., description="Name of the WMS layer to request"),
    width: int = Query(200, description="Width of the thumbnail in pixels"),
    height: int = Query(200, description="Height of the thumbnail in pixels"),
    min_x: float = Query(None, description="Minimum X coordinate of bounding box (EPSG:4326)"),
    min_y: float = Query(None, description="Minimum Y coordinate of bounding box (EPSG:4326)"),
    max_x: float = Query(None, description="Maximum X coordinate of bounding box (EPSG:4326)"),
    max_y: float = Query(None, description="Maximum Y coordinate of bounding box (EPSG:4326)"),
    sld_style: str = Query(None, description="Optional SLD style name to apply to the WMS layer")
):
    """
    Generate a thumbnail with a WMS layer overlaid on OpenStreetMap.
    
    Returns a PNG image directly in the response that can be used in HTML <img> tags.
    """

    # strip the name space
    layer_name = layer_name.partition(":")[2] or layer_name

    # Determine if bbox was provided
    bbox = None
    if all(coord is not None for coord in [min_x, min_y, max_x, max_y]):
        bbox = (min_x, min_y, max_x, max_y)
    
    try:
        # Generate the image and get it as bytes
        image_bytes = create_wms_overlay_bytes(
            wms_url=wms_url,
            layer_name=layer_name,
            bbox=bbox,
            image_size=(width, height),
            sld_style=sld_style
        )
        
        # Return the image directly in the response
        return Response(
            content=image_bytes,
            media_type="image/png",
            headers={
                "Content-Disposition": "inline; filename=wms_thumbnail.png",
                "Cache-Control": "max-age=3600"  # Cache for 1 hour
            }
        )
    
    except Exception as e:
        traceback.print_exc()
        logger.error(f"Error generating WMS thumbnail: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error generating WMS thumbnail: {str(e)}")

def create_wms_overlay_bytes(wms_url, layer_name, bbox=None, image_size=(200, 200), sld_style=None):
    """
    Create a thumbnail image that overlays a WMS layer on top of OpenStreetMap background.
    
    Parameters:
    -----------
    wms_url : str
        URL to the WMS service
    layer_name : str
        Name of the WMS layer to request
    bbox : tuple, optional
        Bounding box in the format (minx, miny, maxx, maxy) in EPSG:4326
        If None, the layer's default bounding box will be used
    image_size : tuple, optional
        Size of the output image as (width, height) in pixels
    sld_style : str, optional
        SLD style name to apply to the WMS layer
        
    Returns:
    --------
    bytes: Image data as bytes that can be directly returned in an HTTP response
    """
    try:
        logger.info(f"Creating WMS overlay for layer {layer_name} from {wms_url}")
        if sld_style:
            logger.info(f"Using SLD style: {sld_style}")
        
        # Connect to the WMS service
        wms = WebMapService(wms_url)
        
        # Get layer information
        layer_info = wms[layer_name]
        
        # If no bbox is provided, use the layer's default
        if bbox is None:
            bbox = layer_info.boundingBoxWGS84
            logger.info(f"Using default bbox: {bbox}")
        else:
            logger.info(f"Using provided bbox: {bbox}")
        
        # Create figure and axis
        fig, ax = plt.subplots(figsize=(4, 4), dpi=100)
        
        # Set up our CRS transformer
        transformer = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
        
        # Transform the bbox to web mercator for consistent display
        minx, miny = transformer.transform(bbox[0], bbox[1])
        maxx, maxy = transformer.transform(bbox[2], bbox[3])
        
        # Set the extent of our plot to the web mercator bbox
        ax.set_xlim(minx, maxx)
        ax.set_ylim(miny, maxy)
        
        # Add the OpenStreetMap basemap
        ctx.add_basemap(ax, source=ctx.providers.OpenStreetMap.Mapnik)
        
        # Prepare the styles parameter for the WMS request
        styles = [sld_style] if sld_style else ['']
        
        # Get the WMS image - request at higher resolution for better quality
        wms_img = wms.getmap(
            layers=[layer_name],
            styles=styles,
            srs='EPSG:3857',
            bbox=(minx, miny, maxx, maxy),
            size=(max(400, image_size[0]*2), max(400, image_size[1]*2)),  # Higher resolution for better quality
            format='image/png',
            transparent=True
        )
        
        # Convert the WMS response to an image
        img = Image.open(BytesIO(wms_img.read()))
        
        # Display the WMS image on top of the basemap
        ax.imshow(img, extent=[minx, maxx, miny, maxy], alpha=0.7)
        
        # Remove axes for cleaner thumbnail
        ax.set_axis_off()
        
        # Add a small title with the layer name
        # plt.title(f"{layer_name}", fontsize=8)
        
        # Save the figure with tight layout
        plt.tight_layout()
        
        # Save to a temporary buffer
        buf = BytesIO()
        plt.savefig(buf, format='png', dpi=100, bbox_inches='tight', pad_inches=0.1)
        plt.close('all')  # Close all figures to prevent memory leaks
        
        # Open the buffer with PIL and resize to exactly the requested size
        buf.seek(0)
        thumbnail = Image.open(buf)
        thumbnail = thumbnail.resize(image_size, Image.LANCZOS)
        
        # Save the final thumbnail to a new buffer and return the bytes
        output_buf = BytesIO()
        thumbnail.save(output_buf, format='PNG')
        output_buf.seek(0)
        
        logger.info(f"Successfully generated thumbnail image")
        
        return output_buf.getvalue()
    
    except Exception as e:
        logger.error(f"Error in create_wms_overlay_bytes: {str(e)}")
        # Re-raise the exception to be caught by the calling function
        raise
