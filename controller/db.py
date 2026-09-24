"""
Database adapter used for each connection.
"""

# from decouple import config
import os
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, exc
from sqlalchemy.orm.session import sessionmaker
from dotenv.main import load_dotenv
# from models.wfr_database import User


load_dotenv('fastapi/.env')

engine = create_engine(
    # "postgresql+psycopg2://" + os.environ['db_user'] + ':' + os.environ['db_password'] + '@' + os.environ['base_host'] + "/" + os.environ['db_name'],
    os.environ['database_connection'],	
    echo=False,
    max_overflow=50,
)

SessionLocal = sessionmaker(bind=engine, future=True)

def use_unencrypted_session():
    db = SessionLocal()
    try:
        yield db
    except Exception as e:
        raise HTTPException(500, f"An unknown error occurred: {str(e)}")
    finally:
        db.close()


def toCRUD(action: str):
    if action == 'C':
        return 'Create'
    elif action == 'R':
        return 'Read'
    elif action == 'U':
        return 'Update'
    elif action == 'D':
        return 'Delete'
    else:
        return None


def expandCRUD(group: str):
    elements = group.split('.')
    crud = elements.pop()
    return ['%s.%s' % ('.'.join(elements), toCRUD(x)) for x in list(crud)]


def get_db():
    db = SessionLocal()
    try:
        yield db
    except exc.ProgrammingError as e:
        raise e
        # raise HTTPException(
        #     500, f"A database-side programming error occurred: {str(e.detail)}"
        # )
    except exc.IntegrityError as e:
        raise HTTPException(
            500, f"A database-side integrity error occurred: {str(e.detail)}"
        )
    except ValidationError as e:
        raise HTTPException(500, detail=e.json())
    finally:
        # end_decrypted_session(db)
        db.close()