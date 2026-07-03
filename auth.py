from passlib.context import CryptContext
import pyotp
import qrcode
import io
import base64
import hashlib
import secrets

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def generate_mfa_secret():
    return pyotp.random_base32()

def get_totp_uri(secret, username, issuer_name="ESXiBackup"):
    return pyotp.totp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer_name)

def verify_totp(secret, code):
    totp = pyotp.totp.TOTP(secret)
    return totp.verify(code)

def generate_qr_code(uri):
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(uri)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    
    buffered = io.BytesIO()
    img.save(buffered)
    return base64.b64encode(buffered.getvalue()).decode()

from jose import JWTError, jwt
from datetime import datetime, timedelta

SECRET_KEY = "super-secure-backup-manager-secret"
ALGORITHM = "HS256"

def create_access_token(username: str):
    expire = datetime.utcnow() + timedelta(days=7)
    to_encode = {"sub": username, "exp": expire}
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def decode_access_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None


def _hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def create_api_key(db, user_id: int, name: str):
    from models import ApiKey

    raw_key = f"nbak_{secrets.token_urlsafe(32)}"
    api_key = ApiKey(user_id=user_id, name=name, key_hash=_hash_api_key(raw_key))
    db.add(api_key)
    db.commit()
    db.refresh(api_key)
    return raw_key, api_key


def verify_api_key(db, raw_key: str):
    from models import ApiKey

    if not raw_key or not raw_key.startswith("nbak_"):
        return None
    api_key = db.query(ApiKey).filter(ApiKey.key_hash == _hash_api_key(raw_key)).first()
    if api_key:
        api_key.last_used_at = datetime.utcnow()
        db.commit()
    return api_key
