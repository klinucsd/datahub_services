import zipfile
from dotenv.main import load_dotenv
from fastapi import HTTPException
import rasterio
import requests
import tempfile
import xml.etree.ElementTree as ET
import rasterio
import geopandas as gpd
import rasterio
import os
from rasterio.mask import mask
from geojson import Polygon
import geojson


def debug_print(msg): 
    from datetime import datetime 
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]                                                                                                
    print(f"[{timestamp}] {msg}")    


load_dotenv('fastapi/env')

class DownloadLayerFunctions:
    
    def __init__(self):
        self.vector_columns = {
                              'boundary:ca_counties' : 'boundary:namelsad', 
                              'boundary:california_local_fire_districts': 'gml:name', 
                              'boundary:rrk_boundaries': "boundary:rrk_region",

                              'boundary:blm_ca_administrative_unit_boundary_field_office_polygon': 'boundary:admu_name',
                              'boundary:forest_administrative_boundaries': 'boundary:forestname',
                              'boundary:cal_fire_operational_units': 'boundary:unit',
                              'boundary:california_state_senate_districts_map_2020': 'gml:name',
                              'boundary:assembly_districts': 'boundary:assemblydi',

			      'boundary:wftf_regions_final_updated_july_2025': 'boundary:region',

                              }
        
        self.vector_layer_names = {
                              'boundary:ca_counties' : 'California Counties', 
                              'boundary:california_local_fire_districts': 'California Local Fire Districts', 
                              'boundary:rrk_boundaries': "Regional Resource Kit Boundaries",

                              'boundary:blm_ca_administrative_unit_boundary_field_office_polygon' : 'BLM CA Administrative Unit Boundary Field Office Polygon' ,
                              'boundary:forest_administrative_boundaries' : 'Administrative Forest Boundaries',
                              'boundary:cal_fire_operational_units' : 'CAL FIRE Operational Units',
                              'boundary:california_state_senate_districts_map_2020' : 'California State Senate Districts',
                              'boundary:assembly_districts' : 'California Assembly Districts',
 
                              'boundary:wftf_regions_final_updated_july_2025': 'California Wildfire & Forest Resilience Task Force Region Boundaries',
                              }

    async def download_layer(self, layer_name, vector_layer_name = None, vector_column_filter = None):    
        geoserver_url = os.environ['geoserver_site']
        if vector_layer_name == None or vector_column_filter == None:
            raster_url = f'{geoserver_url}/ows?service=WCS&version=2.0.0&request=GetCoverage&coverageId={layer_name}&format=image/geotiff'
            with tempfile.NamedTemporaryFile(delete=True, suffix='.tif') as tmp_tif:
                tmp_tif_path = tmp_tif.name 

            response = requests.get(raster_url)
             # Save the path to the temporary TIF file
            with open(tmp_tif_path, 'wb') as f:
                f.write(response.content)
                f.close()

            return tmp_tif_path
        
        else:
            # set static fields
            vector_layer = list(self.vector_layer_names.keys())[list(self.vector_layer_names.values()).index(vector_layer_name)]
            attribute = self.vector_columns[vector_layer].split(':')[-1]

            # Fetch cropping vector
            wcs_url = f"{geoserver_url}/ows?service=wfs&version=1.0.0&request=GetFeature&typeName={vector_layer}&cql_filter={attribute}='{vector_column_filter}'&srsName=EPSG:3310"
            wcs_polygon_xml = requests.get(wcs_url)
            polygon_tree = ET.fromstring(wcs_polygon_xml.text)


            # fetch bbox for initial crop on wcs fetch of raster
            polygons = []
            def bbox(coord_list):
                box = []
                for i in (0,1):
                    res = sorted(coord_list, key=lambda x:x[i])
                    box.append((res[0][i],res[-1][i]))
                ret = [box[0][0], box[1][0], box[0][1], box[1][1]]
                return ret

            bboxes = []
            for outerBoundaryIs in polygon_tree.findall('.//{http://www.opengis.net/gml}Polygon/{http://www.opengis.net/gml}outerBoundaryIs'):
                for coord in outerBoundaryIs.findall(".//{http://www.opengis.net/gml}LinearRing/{http://www.opengis.net/gml}coordinates"):
                    coordinates = coord.text
                    coor_list = coordinates.split(' ')
                    ring_coor = []
                    for coor in coor_list:
                        x,y = coor.split(',')
                        ring_coor.append([float(x),float(y)])

                    polygons.append({"type" : "Polygon", 
                                    "coordinates": [ring_coor]})
                    
                    bboxes.append(bbox(list(geojson.utils.coords(Polygon(ring_coor)))))
                    
            full_extent = [None, None, None, None]
            for box in bboxes:
                if full_extent[0] == None or box[0] < full_extent[0]:
                    full_extent[0] = box[0]
                if full_extent[1] == None or box[1] < full_extent[1]:
                    full_extent[1] = box[1]
                if full_extent[2] == None or box[2] > full_extent[2]:
                    full_extent[2] = box[2]
                if full_extent[3] == None or box[3] > full_extent[3]:
                    full_extent[3] = box[3]

            debug_print(f"full_extent: {full_extent}")

            # fetch raster cropped to vector bbox and save to temp
            raster_url = f'{geoserver_url}/ows?service=WCS&version=2.0.0&request=GetCoverage&coverageId={layer_name}&format=image/geotiff&SUBSET=X({full_extent[0]},{full_extent[2]})&SUBSET=Y({full_extent[1]},{full_extent[3]})&SubsettingCRS=http://www.opengis.net/def/crs/EPSG/0/3310'
            with tempfile.NamedTemporaryFile(delete=True, suffix='.tif') as tmp_tif:
                tmp_tif_path = tmp_tif.name 

            debug_print(f"raster_url: {raster_url}")
            response = requests.get(raster_url)

            if 'Empty intersection after subsetting' in str(response.content):

                layer_name = f"oper:{layer_name}"
                raster_url = f'{geoserver_url}/ows?service=WCS&version=2.0.0&request=GetCoverage&coverageId={layer_name}&format=image/geotiff&SUBSET=X({full_extent[0]},{full_extent[2]})&SUBSET=Y({full_extent[1]},{full_extent[3]})&SubsettingCRS=http://www.opengis.net/def/crs/EPSG/0/3310'
                response = requests.get(raster_url)
                if 'Empty intersection after subsetting' in str(response.content):
                    raise HTTPException(status_code=500, detail="The raster and subsetting vector do not overlap. Please choose a different filter.")

            # Save the path to the temporary TIF file
            with open(tmp_tif_path, 'wb') as f:
                f.write(response.content)
                f.close()

            # create mask around polygons to crop
            with rasterio.open(tmp_tif_path, driver='GTiff') as src:
                out_image, out_transform = mask(src, polygons, crop=True)
            out_meta = src.meta.copy()

            # save the resulting raster  
            out_meta.update({"driver": "GTiff",
                "height": out_image.shape[1],
                "width": out_image.shape[2],
                "transform": out_transform})

            # save to tmp
            with rasterio.open(tmp_tif_path, "w", **out_meta) as dest:
                dest.write(out_image)

            return tmp_tif_path


    def compress_tif(file_path, layer_name, file_name = None):        

        if file_name is None:
            file_name = layer_name

        zip_path = file_name[:-4] + ".zip"
        zf = zipfile.ZipFile(zip_path, mode="w")
        try:
            zf.write(file_path, file_name + '.tif', compress_type=zipfile.ZIP_DEFLATED)             

            try:
                layer_name_norm = layer_name.split(':')[1]
                dbf_file_name = f"/code/tif_vat_dbf_files/{layer_name_norm}.tif.vat.dbf"
                debug_print(f"dbf_file_name: {dbf_file_name}")
                if os.path.exists(dbf_file_name):
                    debug_print("DBF exists")
                    zf.write(dbf_file_name, f"{file_name}.tif.vat.dbf", compress_type=zipfile.ZIP_DEFLATED)
                    debug_print(f"Added DBF file to zip: {dbf_file_name}")	
                else:	
                    debug_print("DBF does not exist")
            except:
                pass             

        except FileNotFoundError:
            print("An error occurred")
        finally:
            zf.close()

        os.remove(file_path)

        return zip_path
        

    async def get_vectors(self):
        geoserver_url = os.environ['geoserver_site']
        feature_values = {}
        for key in self.vector_columns.keys():
            feature_xml = requests.get(f'{geoserver_url}/wfs?service=wfs&version=2.0.0&request=GetFeature&typeNames={key}')

            capabilities_tree = ET.fromstring(feature_xml.text)

            if "boundary" in self.vector_columns[key]:
                col_name = "{" + geoserver_url + '/' + self.vector_columns[key].replace(':', "}")
            else:
                col_name = "{http://www.opengis.net/" + self.vector_columns[key].replace(':', "/3.2}")

            vals = capabilities_tree.findall(f".//{col_name}")

            val_name = [val.text for val in vals]
            val_name = list(set(val_name))
            val_name.sort()


            feature_values[self.vector_layer_names[key]] = val_name

        return feature_values
        
