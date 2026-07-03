from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials, APIKeyCookie
from sqlalchemy.orm import Session
from models import SessionLocal, User, ApiKey
import auth

bearer_scheme = HTTPBearer(auto_error=False)
cookie_sec = APIKeyCookie(name="session_token", auto_error=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _user_from_token(db: Session, token: str) -> User:
    if token.startswith("nbak_"):
        api_key = auth.verify_api_key(db, token)
        if not api_key:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
        user = db.query(User).filter(User.id == api_key.user_id).first()
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key user")
        return user
    username = auth.decode_access_token(token)
    if not username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


def get_api_user(
    db: Session = Depends(get_db),
    bearer: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    cookie_token: str = Depends(cookie_sec),
) -> User:
    token = None
    if bearer:
        token = bearer.credentials
    elif cookie_token:
        token = cookie_token
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return _user_from_token(db, token)


def require_api_role(*allowed_roles):
    def dependency(user: User = Depends(get_api_user)) -> User:
        role = user.role or "admin"
        if role not in allowed_roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
        return user
    return dependency
