"""User management operations for UI and API."""

import secrets
import string

import auth
from models import User, ApiKey


def _gen_temp_password(length=12):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def user_to_dict(user):
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role or "admin",
        "email": user.email or "",
        "is_mfa_enabled": bool(user.is_mfa_enabled),
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


def list_users(db):
    return db.query(User).order_by(User.username).all()


def create_user(db, username, role="operator"):
    if role not in ("admin", "operator", "viewer"):
        raise ValueError("Invalid role")
    existing = db.query(User).filter(User.username == username).first()
    if existing:
        raise ValueError(f"User '{username}' already exists")
    temp_pw = _gen_temp_password()
    user = User(
        username=username,
        hashed_password=auth.get_password_hash(temp_pw),
        role=role,
        is_mfa_enabled=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user, temp_pw


def delete_user(db, user_id, current_username):
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise ValueError("User not found")
    if target.username == current_username:
        raise ValueError("Cannot delete your own account")
    db.delete(target)
    db.commit()
    return target


def reset_password(db, user_id):
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise ValueError("User not found")
    temp_pw = _gen_temp_password()
    target.hashed_password = auth.get_password_hash(temp_pw)
    db.commit()
    return target, temp_pw


def reset_mfa(db, user_id):
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise ValueError("User not found")
    target.is_mfa_enabled = False
    target.mfa_secret = None
    db.commit()
    return target


def update_role(db, user_id, role, current_username):
    if role not in ("admin", "operator", "viewer"):
        raise ValueError("Invalid role")
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise ValueError("User not found")
    if target.username == current_username:
        raise ValueError("Cannot change your own role via API")
    target.role = role
    db.commit()
    db.refresh(target)
    return target


def update_profile(db, username, email=None, notify_subscriptions=None):
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise ValueError("User not found")
    if email is not None:
        user.email = email.strip()
    if notify_subscriptions is not None:
        user.notify_subscriptions = notify_subscriptions.strip()
    db.commit()
    db.refresh(user)
    return user


def revoke_all_api_keys(db, user_id):
    db.query(ApiKey).filter(ApiKey.user_id == user_id).delete()
    db.commit()
