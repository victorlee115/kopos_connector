import base64
import hashlib

from Crypto.Cipher import AES


def encrypt_pin(pin: str, username: str) -> str:
    """PBKDF2-HMAC-SHA1 matching CryptoJS.PBKDF2(pin, username, {keySize:32, iterations:1000}).

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
    pad_len = decrypted[-1]
    return decrypted[:-pad_len].decode("utf-8")
