import os
from cryptography.fernet import Fernet
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
