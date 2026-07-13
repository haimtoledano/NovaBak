import os
import base64
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from config_env import DATA_DIR

class SecretManager:
    _fernet = None

    @classmethod
    def _get_fernet(cls):
        if cls._fernet is not None:
            return cls._fernet

        # Read or generate the DB_ENCRYPTION_KEY
        key = os.environ.get("DB_ENCRYPTION_KEY")
        secret_file = os.path.join(DATA_DIR, ".db_secret")
        
        if not key:
            if os.path.exists(secret_file):
                with open(secret_file, "r") as f:
                    key = f.read().strip()
            else:
                key = Fernet.generate_key().decode()
                try:
                    with open(secret_file, "w") as f:
                        f.write(key)
                except OSError:
                    pass

        cls._fernet = Fernet(key.encode())
        return cls._fernet

    @classmethod
    def encrypt(cls, plaintext: str) -> str:
        """Encrypts a plaintext string and prefixes it with 'enc:'"""
        if not plaintext:
            return plaintext
        if plaintext.startswith("enc:"):
            return plaintext
        encrypted = cls._get_fernet().encrypt(plaintext.encode()).decode()
        return f"enc:{encrypted}"

    @classmethod
    def decrypt(cls, ciphertext: str) -> str:
        """Decrypts a ciphertext string if it is prefixed with 'enc:', else returns it directly."""
        if not ciphertext or not ciphertext.startswith("enc:"):
            return ciphertext
        raw_ciphertext = ciphertext[4:]
        try:
            decrypted = cls._get_fernet().decrypt(raw_ciphertext.encode()).decode()
            return decrypted
        except Exception:
            # If decryption fails, it may have been corrupted or key changed. Return original.
            return ciphertext

    @staticmethod
    def get_stream_cipher(passphrase: str, iv: bytes):
        """
        Returns a (encryptor, decryptor) tuple for AES-256-CTR mode.
        
        passphrase: any string (password, base64 key, or raw key).
                    A proper 32-byte AES key is derived using PBKDF2.
        iv:         16-byte initialization vector (nonce).
                    CTR mode requires the exact same IV for decryption.
        """
        if not passphrase:
            return None, None
            
        try:
            from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
            from cryptography.hazmat.primitives import hashes
            
            # Derive a proper 32-byte AES-256 key from the passphrase
            # Using a fixed salt ensures the same passphrase always produces the same key
            # (required for decryption to work across sessions)
            fixed_salt = b'NovaBak_AES256_v1'
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=fixed_salt,
                iterations=100_000,
                backend=default_backend(),
            )
            key = kdf.derive(passphrase.encode('utf-8'))
                
            cipher = Cipher(algorithms.AES(key), modes.CTR(iv), backend=default_backend())
            return cipher.encryptor(), cipher.decryptor()
        except Exception as e:
            from logger_util import log_error
            log_error(f"Failed to initialize stream cipher: {e}")
            return None, None
