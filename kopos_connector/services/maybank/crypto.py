import base64
import hashlib

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad


def encrypt_pin(pin: str, username: str) -> str:
    """Match the provider's legacy CryptoJS PBKDF2 (explicit SHA1, 1000 iterations).

    keySize:32 means 32 * 4 = 128 bytes derived key.
    .toString() gives hex string.  .substring(0,64) takes first 64 hex chars.
    """
    raw = hashlib.pbkdf2_hmac(
        "sha1",
        pin.encode("utf-8"),
        username.encode("utf-8"),
        1000,
        dklen=128,
    )
    return raw.hex()[:64]


def decrypt_aes(encrypted_b64: str, passphrase: str) -> str:
    """AES-CBC decrypt matching CryptoJS.AES.decrypt(ciphertext, passphrase).

    Uses OpenSSL-compatible EVP_BytesToKey (MD5) for key derivation.
    Input: base64("Salted__" + salt + ciphertext)
    Validate PKCS#7 padding; this does not authenticate AES-CBC ciphertext.
    """
    raw = base64.b64decode(encrypted_b64)
    if raw[:8] != b"Salted__":
        raise ValueError("Invalid encrypted data: missing OpenSSL salt header")
    salt = raw[8:16]
    ciphertext = raw[16:]

    pass_bytes = passphrase.encode("utf-8")
    d = b""
    key_iv = b""
    while len(key_iv) < 48:
        d = hashlib.md5(d + pass_bytes + salt).digest()
        key_iv += d

    key = key_iv[:32]
    iv = key_iv[32:48]

    cipher = AES.new(key, AES.MODE_CBC, iv)
    decrypted = cipher.decrypt(ciphertext)
    return unpad(decrypted, AES.block_size).decode("utf-8")
