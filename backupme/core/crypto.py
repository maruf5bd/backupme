import os
import hashlib
import secrets

_AES_KEY_SIZE = 32
_SALT_SIZE = 16
_NONCE_SIZE = 12
_TAG_SIZE = 16

_PBKDF2_ITER = 600_000

_ARGON2_TIME_COST = 3
_ARGON2_MEM_COST = 65536
_ARGON2_PARALLELISM = 4


def _derive_key(password: bytes, salt: bytes) -> bytes:
    try:
        from argon2.low_level import hash_secret_raw, Type
        return hash_secret_raw(
            secret=password,
            salt=salt,
            time_cost=_ARGON2_TIME_COST,
            memory_cost=_ARGON2_MEM_COST,
            parallelism=_ARGON2_PARALLELISM,
            hash_len=_AES_KEY_SIZE,
            type=Type.ID,
        )
    except ImportError:
        return hashlib.pbkdf2_hmac("sha256", password, salt, _PBKDF2_ITER, dklen=_AES_KEY_SIZE)


def generate_key() -> bytes:
    return secrets.token_bytes(_AES_KEY_SIZE)


def encrypt(data: bytes, key: bytes) -> tuple[bytes, bytes, bytes]:
    salt = secrets.token_bytes(_SALT_SIZE)
    derived_key = _derive_key(key, salt)
    nonce = secrets.token_bytes(_NONCE_SIZE)
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        aesgcm = AESGCM(derived_key)
        ct = aesgcm.encrypt(nonce, data, None)
        return salt + nonce + ct, nonce, ct[-16:]
    except ImportError:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        cipher = Cipher(algorithms.AES(derived_key), modes.GCM(nonce))
        encryptor = cipher.encryptor()
        ct = encryptor.update(data) + encryptor.finalize()
        tag = encryptor.tag
        return salt + nonce + tag + ct, nonce, tag


def decrypt(data: bytes, key: bytes) -> bytes:
    salt = data[:_SALT_SIZE]
    nonce = data[_SALT_SIZE:_SALT_SIZE + _NONCE_SIZE]
    rest = data[_SALT_SIZE + _NONCE_SIZE:]
    derived_key = _derive_key(key, salt)
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        aesgcm = AESGCM(derived_key)
        return aesgcm.decrypt(nonce, rest, None)
    except ImportError:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        tag = rest[:_TAG_SIZE]
        ct = rest[_TAG_SIZE:]
        cipher = Cipher(algorithms.AES(derived_key), modes.GCM(nonce, tag))
        decryptor = cipher.decryptor()
        return decryptor.update(ct) + decryptor.finalize()


def load_key(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read().strip()


def save_key(key: bytes, path: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(key + b"\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass