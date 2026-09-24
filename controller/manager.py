import os

from fastapi_login import LoginManager

from controller.db import SessionLocal
from models.wfr_database import User
from sqlalchemy.orm import lazyload

SECRET = os.environ['login_manager_secret']
manager = LoginManager(SECRET, '/staging-api/v1/Auth/login')

@manager.user_loader()
def get_user_by_username(username: str):
    return SessionLocal().query(User).filter(User.username == username).options(lazyload(User.roles)).first()

def get_user_by_email(email:str):
    return SessionLocal().query(User).filter(User.email == email).options(lazyload(User.roles)).first()