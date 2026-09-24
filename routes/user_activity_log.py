import logging
import traceback

from sqlalchemy import Column, Integer, String, Text, JSON, TIMESTAMP, func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import Session

from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List

from controller.manager import manager
from fastapi import Depends, APIRouter, HTTPException, Request
from controller.db import get_db


router = APIRouter(tags=["Utility"], prefix='/Utility')

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Set up the database model
Base = declarative_base()

class UserActivityLog(Base):
    __tablename__ = "user_activity_logs"
    __table_args__ = {"schema": "hub_catalog"}

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False)
    action_type = Column(String(100), nullable=False)
    action_target = Column(String(512), nullable=False)
    action_detail = Column(JSON)
    ip_address = Column(String(45))
    user_agent = Column(Text)
    timestamp = Column(TIMESTAMP(timezone=True), server_default=func.now(), index=True)

# Set up the pydantic model
class UserActivityLogRequest(BaseModel):
    action_type: str
    action_target: str
    action_detail: Optional[Dict[str, Any]] = None
    
    class Config:
        schema_extra = {
            "example": {
                "action_type": "dataset_download",
                "action_target": "clm-fire-return-interval-departure-frid-mean-condition-class",
                "action_detail": "{\"size\":\"5.8MB\"}",
            }
        }


@router.post("/log_activity", include_in_schema=True)
async def log_activity(request: Request,
                       user_activity: UserActivityLogRequest, 
                       user=Depends(manager), 
                       db:Session=Depends(get_db)):

    try:
        # Create a new log entry
        new_log = UserActivityLog(
            user_id=user.user_id,
            action_type=user_activity.action_type,
            action_target=user_activity.action_target,
            action_detail=user_activity.action_detail,
            ip_address=request.client.host,
            user_agent=request.headers.get("user-agent")
        )
        
        # Add to database
        db.add(new_log)
        db.commit()
        
        logger.info(f"Activity logged for user {user.user_id}: {user_activity.action_type}")
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Error logging activity: {e}")
        logger.error(traceback.format_exc())
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to log activity: str(e)")
