from fastapi import FastAPI, APIRouter, HTTPException, Request, Body, Response
import json

from fastapi.responses import StreamingResponse
from controller.download_layer import DownloadLayerFunctions
from controller.login import LoginFunctions
from controller.download import DownloadMap
from controller.db import get_db
from fastapi import Depends
from sqlalchemy.orm import Session
from controller.manager import manager
from fastapi.encoders import jsonable_encoder
import datetime
from models.wfr_database import User

import traceback


def debug_print(msg):
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    print(f"[{timestamp}] {msg}")


router = APIRouter(
    prefix='/v1/download',
    tags = ['download']
)


@router.get("/download_layer/{layer_name}/{file_name}")
async def download_layer(layer_name: str, file_name: str, \
                         vector_layer_name:str = None, vector_column_filter:str = None):
    '''
          layer_name : raster layer name \n
          file_name : name of tif file returned \n
          vector_layer_name : Name of cropping vector layer \n
          vector_column_filter : filter attribute for vector layer \n
          \n
          Use the get_vector function to find all filter options

    '''

#     if user == None: 
#          raise HTTPException(status_code=500, detail="User Not Found")
    
    vector_layer_name = None if vector_layer_name == 'undefined' else vector_layer_name
    vector_column_filter = None if vector_column_filter == 'undefined' else vector_column_filter

    debug_print(f"layer_name: {layer_name}")

    try:        
        downloadFunctions = DownloadLayerFunctions()
        temp_path = await downloadFunctions.download_layer(layer_name, vector_layer_name, vector_column_filter)
    except Exception as e:
        traceback.print_exc()
        raise e

    debug_print(f"temp_path: {temp_path}")

    zip_path = DownloadLayerFunctions.compress_tif(temp_path, layer_name, file_name)

    zip_filename = zip_path.split('/')[-1]

    CHUNK_SIZE = 1024 * 1024  # = 1MB - adjust the chunk size as desired
    headers = {'Content-Disposition': f'attachment; filename="{zip_filename}"'}

    return StreamingResponse(DownloadMap.iterfile(zip_path, CHUNK_SIZE), headers=headers, media_type='application/zip')

   
@router.get("/get_vectors")
# async def get_vectors(user=Depends(manager)):
async def get_vectors():
#     if user == None: 
#          raise HTTPException(status_code=500, detail="User Not Found")
    
    downloadfunctions = DownloadLayerFunctions()
    
    vector_list = await downloadfunctions.get_vectors()

    return vector_list
   

# @router.get("/download_as_shape/{layer_name}")
# async def download_as_shape(layer_name: str, polygon_clipping = None, user=Depends(manager)):
    
#     if user == None: 
#          raise HTTPException(status_code=500, detail="User Not Found")
    
#     temp_path = await DownloadLayerFunctions.download_to_shape(layer_name, polygon_clipping)

#     zip_path = DownloadMap.compress(temp_path, layer_name)

#     zip_filename = zip_path.split('/')[-1]

#     CHUNK_SIZE = 1024 * 1024  # = 1MB - adjust the chunk size as desired
#     headers = {'Content-Disposition': f'attachment; filename="{zip_filename}"'}

#     return StreamingResponse(DownloadMap.iterfile(zip_path, CHUNK_SIZE), headers=headers, media_type='application/zip')
   

