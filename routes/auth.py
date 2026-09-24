import bcrypt
from typing import Annotated
from fastapi import Depends, APIRouter, FastAPI, HTTPException, Request, Body, Response
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.security import OAuth2PasswordBearer
from fastapi_login.exceptions import InvalidCredentialsException
from pydantic import BaseModel
from controller.login import LoginFunctions
from models.wfr_database import User
from models.wfr_pydantic import Account
from controller.db import SessionLocal, get_db
from fastapi import Depends
from sqlalchemy.orm import Session, joinedload
from controller.manager import get_user_by_username, get_user_by_email, manager
import os
import jwt
import json
from datetime import datetime, timedelta

from models.wfr_database import UserRole, Role, User
from routes.ckan import send_email_via_sparcal

router = APIRouter(tags=["Auth"], prefix='/v1/Auth')
oauth2_scheme_token = OAuth2PasswordBearer(tokenUrl="token")


base_url = os.environ['backend_full_url']

def send_verification_email(admin, user, access_token):
    result = send_email_via_sparcal(
        to_email=admin.email,	
        subject=f"New Task Force Data Hub Account Verification Request",	
        body=f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Account Verification</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            line-height: 1.6;
            color: #333;
            margin: 0;
            padding: 20px;
            background-color: #f5f5f5;
        }}
        .container {{
            max-width: 600px;
            margin: 0 auto;
            background-color: #ffffff;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
        }}
        .header {{
            background-color: #2c3e50;
            color: white;
            padding: 15px 20px;
            border-radius: 6px;
            margin-bottom: 25px;
        }}
        .section {{
            margin-bottom: 25px;
        }}
        .label {{
            font-weight: bold;
            color: #2c3e50;
            margin-bottom: 10px;
            display: block;
            font-size: 16px;
        }}
        .content {{
            background: #f8f9fa;
            padding: 15px;
            border-radius: 6px;
            border: 1px solid #e9ecef;
        }}
        ul {{
            list-style-type: none;
            padding-left: 0;
            margin: 0;
        }}
        li {{
            margin-bottom: 10px;
            padding-left: 10px;
        }}
        .field-label {{
            color: #666;
            width: 100px;
            display: inline-block;
        }}
        .field-value {{
            color: #333;
            font-weight: 500;
        }}
        .verification-link {{
            display: inline-block;
            margin-top: 20px;
            padding: 12px 24px;
            background-color: #2c3e50;
            color: white;
            text-decoration: none;
            border-radius: 4px;
            font-weight: 500;
        }}
        .message {{
            margin-bottom: 20px;
            line-height: 1.6;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2 style="margin: 0;">New Account Verification Request</h2>
        </div>
        <div class="message">
            <p>Hello Admin,</p>
            <p>A new account has been added to the Task Force Data Hub.</p>
        </div>
        <div class="section">
            <span class="label">User Information</span>
            <div class="content">
                <ul>
                    <li>
                        <span class="field-label">Name:</span>
                        <span class="field-value">{user.first_name} {user.last_name}</span>
                    </li>
                    <li>
                        <span class="field-label">Username:</span>
                        <span class="field-value">{user.username}</span>
                    </li>
                    <li>
                        <span class="field-label">Email:</span>
                        <span class="field-value">{user.email}</span>
                    </li>
                    <li>
                        <span class="field-label">Agency:</span>
                        <span class="field-value">{user.affiliation}</span>
                    </li>
                </ul>
            </div>
        </div>
        <div class="section">
            <span class="label">Verification Action</span>
            <div class="content">
                <p>To approve this account, please click the link below:</p>
                <a href="{base_url}/Auth/validate_account/{user.username}/{access_token}" 
                   class="verification-link">
                    Verify Account
                </a>
            </div>
        </div>
    </div>
</body>
</html>
              """
        )
    return result


def send_verification_email_to_user(user, access_token):
    result = send_email_via_sparcal(
        to_email=user.email,	
        subject=f"New Task Force Data Hub Account Verification Request",	
        body=f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Account Verification</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            line-height: 1.6;
            color: #333;
            margin: 0;
            padding: 20px;
            background-color: #f5f5f5;
        }}
        .container {{
            max-width: 600px;
            margin: 0 auto;
            background-color: #ffffff;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
        }}
        .header {{
            background-color: #2c3e50;
            color: white;
            padding: 15px 20px;
            border-radius: 6px;
            margin-bottom: 25px;
        }}
        .section {{
            margin-bottom: 25px;
        }}
        .label {{
            font-weight: bold;
            color: #2c3e50;
            margin-bottom: 10px;
            display: block;
            font-size: 16px;
        }}
        .content {{
            background: #f8f9fa;
            padding: 15px;
            border-radius: 6px;
            border: 1px solid #e9ecef;
        }}
        ul {{
            list-style-type: none;
            padding-left: 0;
            margin: 0;
        }}
        li {{
            margin-bottom: 10px;
            padding-left: 10px;
        }}
        .field-label {{
            color: #666;
            width: 100px;
            display: inline-block;
        }}
        .field-value {{
            color: #333;
            font-weight: 500;
        }}
        .verification-link {{
            display: inline-block;
            margin-top: 20px;
            padding: 12px 24px;
            background-color: lightgray;
            color: white;
            text-decoration: none;
            border-radius: 4px;
            font-weight: 500;
        }}
        .message {{
            margin-bottom: 20px;
            line-height: 1.6;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2 style="margin: 0;">New Account Verification Request</h2>
        </div>
        <div class="message">
            <p>Hello {user.first_name},</p>
            <p>A new account has been added to the Task Force Data Hub.</p>
        </div>
        <div class="section">
            <span class="label">User Information</span>
            <div class="content">
                <ul>
                    <li>
                        <span class="field-label">Name:</span>
                        <span class="field-value">{user.first_name} {user.last_name}</span>
                    </li>
                    <li>
                        <span class="field-label">Username:</span>
                        <span class="field-value">{user.username}</span>
                    </li>
                    <li>
                        <span class="field-label">Email:</span>
                        <span class="field-value">{user.email}</span>
                    </li>
                    <li>
                        <span class="field-label">Agency:</span>
                        <span class="field-value">{user.affiliation}</span>
                    </li>
                </ul>
            </div>
        </div>
        <div class="section">
            <span class="label">Verification Action</span>
            <div class="content">
                <p>To activiate this account, please click the link below:</p>
                <a href="{base_url}/Auth/validate_account/{user.username}/{access_token}" 
                   class="verification-link">
                    Verify Account
                </a>
            </div>
        </div>
    </div>
</body>
</html>
              """
        )
    return result


def send_approval_email(user):
    result = send_email_via_sparcal(
        to_email=user.email,	
        subject=f"Task Force Data Hub Account Approved",	
        body=f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Account Approved</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            line-height: 1.6;
            color: #333;
            margin: 0;
            padding: 20px;
            background-color: #f5f5f5;
        }}
        .container {{
            max-width: 600px;
            margin: 0 auto;
            background-color: #ffffff;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
        }}
        .header {{
            background-color: #2c3e50;
            color: white;
            padding: 15px 20px;
            border-radius: 6px;
            margin-bottom: 25px;
        }}
        .section {{
            margin-bottom: 25px;
        }}
        .label {{
            font-weight: bold;
            color: #2c3e50;
            margin-bottom: 10px;
            display: block;
            font-size: 16px;
        }}
        .content {{
            background: #f8f9fa;
            padding: 15px;
            border-radius: 6px;
            border: 1px solid #e9ecef;
        }}
        ul {{
            list-style-type: none;
            padding-left: 0;
            margin: 0;
        }}
        li {{
            margin-bottom: 10px;
            padding-left: 10px;
        }}
        .field-label {{
            color: #666;
            width: 100px;
            display: inline-block;
        }}
        .field-value {{
            color: #333;
            font-weight: 500;
        }}
        .verification-link {{
            display: inline-block;
            margin-top: 20px;
            padding: 12px 24px;
            background-color: #2c3e50;
            color: white;
            text-decoration: none;
            border-radius: 4px;
            font-weight: 500;
        }}
        .message {{
            margin-bottom: 20px;
            line-height: 1.6;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2 style="margin: 0;">Task Force Data Hub Account Approved</h2>
        </div>
        <div class="message">
            <p>Hello {user.first_name} {user.last_name},</p>
            <p>Your Task Force Data Hub account has been approved. You now have access to the Data Hub. </p>
        </div>
        <div class="section">
            <span class="label">Access Action</span>
            <div class="content">
                <a href="{base_url}" class="verification-link">
                    Login
                </a>
            </div>
        </div>
    </div>
</body>
</html>
              """
        )
    return result


def send_reset_password_email(user, access_token):
    result = send_email_via_sparcal(
        to_email=user.email,    
        subject=f"Reset Task Force Data Hub Account Password",    
        body=f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Reset Your Password - Task Force Data Hub</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            line-height: 1.6;
            color: #333;
            margin: 0;
            padding: 20px;
            background-color: #f5f5f5;
        }}
        .container {{
            max-width: 600px;
            margin: 0 auto;
            background-color: #ffffff;
            padding: 40px;
            border-radius: 12px;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
        }}
        .logo {{
            text-align: center;
            margin-bottom: 30px;
        }}
        .logo img {{
            max-height: 60px;
        }}
        .header {{
            background-color: #003366;
            color: white;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 30px;
            text-align: center;
        }}
        .header h2 {{
            margin: 0;
            font-size: 24px;
            font-weight: 600;
        }}
        .message {{
            margin-bottom: 30px;
            line-height: 1.8;
            color: #444;
        }}
        .button-container {{
            text-align: center;
            margin: 35px 0;
        }}
        .reset-button {{
            display: inline-block;
            padding: 14px 32px;
            background-color: #005DA6;
            color: white;
            text-decoration: none;
            border-radius: 6px;
            font-weight: 500;
            font-size: 16px;
            transition: background-color 0.2s;
        }}
        .reset-button:hover {{
            background-color: #004d8c;
        }}
        .security-notice {{
            background-color: #f8f9fa;
            padding: 20px;
            border-radius: 8px;
            border: 1px solid #e9ecef;
            margin-top: 30px;
            font-size: 14px;
            color: #666;
        }}
        .footer {{
            margin-top: 40px;
            text-align: center;
            font-size: 14px;
            color: #666;
            border-top: 1px solid #eee;
            padding-top: 20px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h2>Password Reset Request</h2>
        </div>
        <div class="message">
            <p>Hello {user.first_name},</p>
            <p>We received a request to reset the password for your Task Force Data Hub account. To proceed with the password reset, please click the button below:</p>
        </div>
        <div class="button-container">
            <a href="https://wlrdatahub.sdsc.edu/password-reset-form.html?token={access_token}" 
               class="reset-button">
                Reset Your Password
            </a>
        </div>
        <div class="security-notice">
            <p><strong>Security Notice:</strong></p>
            <ul style="margin: 0; padding-left: 20px;">
                <li>This link will expire in 24 hours for security purposes.</li>
                <li>If you didn't request this password reset, please ignore this email or contact support if you have concerns.</li>
                <li>For your security, the link can only be used once.</li>
            </ul>
        </div>
        <div class="footer">
            <p>This is an automated message from Task Force Data Hub. Please do not reply to this email.</p>
            <p>© 2025 Task Force Data Hub. All rights reserved.</p>
        </div>
    </div>
</body>
</html>
              """
        )
    return result



@router.post('/login')
def login(data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    username = data.username
    password = data.password

    user = get_user_by_username(username)
    if not user:
        user = get_user_by_email(username)
        username = user.username
        if not user:  
            raise InvalidCredentialsException
    elif user.is_verified == False:
        raise HTTPException(status_code=500, detail='Account has not been verified')
    else:
        password = password.encode('utf-8')
        pwd_hash = user.password_hash.encode('utf-8')

        if not bcrypt.checkpw(password, pwd_hash):
            raise InvalidCredentialsException
        
    # LoginFunctions.update_last_login(username, db)

    access_token = manager.create_access_token(
        data={'sub': username}, expires=(timedelta(days = 1))
    )

    rolenames = [ role.name for role in user.roles]

    return {'access_token': access_token, 'roles': rolenames }


@router.post("/user")
async def user(data: Account, db: Session = Depends(get_db)):
    
    user = get_user_by_username(data.username)
    if user != None:
        raise HTTPException(status_code=500, detail='Username is already taken.')    

    email_user = db.query(User).filter(User.email == data.email).first()                                                                                        
    if email_user:                                                                                                                                              
        raise HTTPException(status_code=400, detail='An account with the same email exists')    

    LoginFunctions.create_user(data, db)
    user = get_user_by_username(data.username)

    access_token = manager.create_access_token(
        data={'sub': user.username},
        expires=(timedelta(days = 3))
    )
    send_verification_email_to_user(user, access_token)

    # LoginFunctions.send_verification_email(user, access_token)
    return {"status": 'success', "message": 
            'Account Created.  You will receive an email to verify your email address.'}



async def user2(data: Account, db: Session = Depends(get_db)):
    
    user = get_user_by_username(data.username)
    if user != None:
        raise HTTPException(status_code=500, detail='Username is already taken.')    

    email_user = db.query(User).filter(User.email == data.email).first()                                                                                        
    if email_user:                                                                                                                                              
        raise HTTPException(status_code=400, detail='An account with the same email exists')    

    LoginFunctions.create_user(data, db)
    user = get_user_by_username(data.username)

    admin_username = os.environ['admin_username']
    admin = get_user_by_username(admin_username)
    access_token = manager.create_access_token(
        # data={'sub': admin.username},
        data={'sub': user.username},
        expires=(timedelta(days = 3))
    )

    admin_users = (
        db.query(User)
        .join(UserRole, User.user_id == UserRole.user_id)
        .join(Role, UserRole.role_id == Role.role_id)
        .filter(Role.name == 'admin')
        .distinct()  # Ensure unique users even if they have multiple roles
        .all()
    )

    for admin in admin_users:
        send_verification_email(admin, user, access_token)

    try:
        llkay = get_user_by_username("ialtintas")
        if user != None:
            send_verification_email(llkay, user, access_token)   
    except:
        pass    

    # LoginFunctions.send_verification_email(user, access_token)
    return {"status": 'success', "message": 
            'Account Created. Please wait for the admin to verify your account. You will receive and email when it has been approved.'}


@router.post("/forget_password")
async def forget_password(email, db:Session = Depends(get_db)):
    
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=500, detail='No account associated with this email.')    

    access_token = manager.create_access_token(
        data={'sub': user.username},
        expires=(timedelta(days = 3))
    ) 
        
    send_reset_password_email(user, access_token)

    return {"status": 'success', 
            "message": 'An email for resetting the password has been sent.'}


@router.post("/reset_password")
async def reset_password(password, token, db:Session = Depends(get_db)):
    
    jwt_val = jwt.decode(token, manager.secret.secret_for_decode, algorithms=[manager.algorithm]) 
    user = db.query(User).filter(User.username == jwt_val['sub']).first()
    password_hash = LoginFunctions.create_hash_password(password)
    password_hash = str(password_hash)[1:]
    password_hash = password_hash[1:-1]
    user.password_hash = password_hash
    db.commit()      
    db.refresh(user)
    return {"status": 'success', 
            "message": 'The password has been reset.'}



# @router.get("/verify_account/{username}/{token}")
async def verify_account_2(username: str, token: str, db: Session = Depends(get_db)):

    jwt_val = jwt.decode(token, manager.secret.secret_for_decode, algorithms=[manager.algorithm])
    user = get_user_by_username(jwt_val['sub'])
    if any(role.name == 'admin' for role in user.roles) == False:
        raise HTTPException(status_code=500, detail='Unauthorized access')

    user = db.query(User).filter(User.username == username).first()
    if user and not user.is_verified:
        user.is_verified = True
        db.commit()	

        admin_users = (
            db.query(User)
            .join(UserRole, User.user_id == UserRole.user_id)
            .join(Role, UserRole.role_id == Role.role_id)
            .filter(Role.name == 'admin')
            .distinct()  # Ensure unique users even if they have multiple roles
            .all()
        )

        for admin in admin_users:
            send_approval_email(admin, user)

        return { 
                 "status": "success", 
                 "message": f"Account {username} has been verified. An email was sent to the account's provided email." 
               }
    else:
        return { 
                 "status": "success", 
                 "message": f"Account {username} has been verified by other admin." 
               }

    # return LoginFunctions.verify_account(username, db)


@router.get("/verify_account/{username}/{token}")
async def verify_account(username: str, token: str, db: Session = Depends(get_db)):

    jwt_val = jwt.decode(token, manager.secret.secret_for_decode, algorithms=[manager.algorithm])
    if jwt_val['sub'] != username:
        raise HTTPException(status_code=500, detail=f'Unauthorized access')

    user = db.query(User).filter(User.username == username).first()
    if user and not user.is_verified:
        user.is_verified = True
        db.commit()	

        admin_users = (
            db.query(User)
            .join(UserRole, User.user_id == UserRole.user_id)
            .join(Role, UserRole.role_id == Role.role_id)
            .filter(Role.name == 'admin')
            .distinct()  # Ensure unique users even if they have multiple roles
            .all()
        )

        # for admin in admin_users:
        #    send_approval_email(admin, user)
        send_approval_email(user)

        return { 
                 "status": "success", 
                 "message": f"Account {username} has been verified. An email was sent to the account's provided email." 
               }
    else:
        return { 
                 "status": "success", 
                 "message": f"Account {username} has been verified by other admin." 
               }

    # return LoginFunctions.verify_account(username, db)


