"""Synthetic cross-implementation vectors; no provider, network or test-time CLI.

AES fixtures: LibreSSL 3.3.6 `openssl enc -aes-256-cbc -md md5 -salt
-S 0102030405060708 -pass pass:synthetic-provider-passphrase`; invalid padding
uses -nopad with the indicated raw tail. PIN fixtures: Node 24 pbkdf2Sync with
explicit SHA1, 1000 iterations and 128 derived bytes, first 64 hex characters.
These legacy algorithms are provider compatibility, not new crypto guidance.
"""

import base64

import pytest

from kopos_connector.services.maybank.crypto import decrypt_aes, encrypt_pin


PASSPHRASE = "synthetic-provider-passphrase"
VALID_VECTORS = [
    ("", "U2FsdGVkX18BAgMEBQYHCPF+lR1IFbsxDwW+6YmH4fs="),
    ("x", "U2FsdGVkX18BAgMEBQYHCLFOl3zZokIMBeSFz8tjmeY="),
    ("xx", "U2FsdGVkX18BAgMEBQYHCP19VpfyEYOkHDZaTyFr4K8="),
    ("xxx", "U2FsdGVkX18BAgMEBQYHCMwF2tkgYX+lxVTqhb1mNJA="),
    ("xxxx", "U2FsdGVkX18BAgMEBQYHCCAgPvXD6Nbb+piAXP26tRk="),
    ("xxxxx", "U2FsdGVkX18BAgMEBQYHCMJuflolvvl2R1mje0dsRMw="),
    ("xxxxxx", "U2FsdGVkX18BAgMEBQYHCI+QukKwTPoT38HsvF+IN4U="),
    ("xxxxxxx", "U2FsdGVkX18BAgMEBQYHCL3Nh5QUPEmyWfo6rAIp1Jg="),
    ("xxxxxxxx", "U2FsdGVkX18BAgMEBQYHCI2T/LX9ljDAbLAfXiA4BD8="),
    ("xxxxxxxxx", "U2FsdGVkX18BAgMEBQYHCEStgm3Hrboi8cGE4M9ef3U="),
    ("xxxxxxxxxx", "U2FsdGVkX18BAgMEBQYHCKIirtGTSjsHxh2AD4+lXvQ="),
    ("xxxxxxxxxxx", "U2FsdGVkX18BAgMEBQYHCENLzw4gaILKAlzwV3hCytQ="),
    ("xxxxxxxxxxxx", "U2FsdGVkX18BAgMEBQYHCDKQ9iaqN33HUIXtz9mm3Gg="),
    ("xxxxxxxxxxxxx", "U2FsdGVkX18BAgMEBQYHCKjtZyPXq0UR4ypCjt9Q72k="),
    ("xxxxxxxxxxxxxx", "U2FsdGVkX18BAgMEBQYHCIVjj8oCT0hA8T9IBGGz/Ac="),
    ("xxxxxxxxxxxxxxx", "U2FsdGVkX18BAgMEBQYHCDghPTZGV7Yfyagx9a/gSOQ="),
    ("x" * 16, "U2FsdGVkX18BAgMEBQYHCGrMvq3CjKZGbMOwD9T7Bqnnx/biic+nq8JVPhR4XF1G"),
    ("x" * 31, "U2FsdGVkX18BAgMEBQYHCGrMvq3CjKZGbMOwD9T7BqlJ86yB/D+LHhKRt41wLUZK"),
    ("x" * 32, "U2FsdGVkX18BAgMEBQYHCGrMvq3CjKZGbMOwD9T7Bqn1jjv42ago9G9fb2jT1mhc/owyp0Fn8cLl43Nkxeii1A=="),
    ("Kopi ☕ / 茶 / café\x00end", "U2FsdGVkX18BAgMEBQYHCEH59kchMd2OkzyJyHUQw76ll6P7/Icay5CL3O8UCasq"),
]
INVALID_PADDING = [
    ("zero", "U2FsdGVkX18BAgMEBQYHCH1g1C4a+NuVh8E9f/gn7W4="),
    ("17", "U2FsdGVkX18BAgMEBQYHCAL8aaEc1+S7n+fyBH19Of0="),
    ("255", "U2FsdGVkX18BAgMEBQYHCLrs9x7NbqhWtKa9jl2ELNk="),
    ("mismatched-02", "U2FsdGVkX18BAgMEBQYHCHpS3nfYWVHUCskgkbR1Oqg="),
    ("17-byte-tail", "U2FsdGVkX18BAgMEBQYHCHBfMW6qu/5z0HQ1Mj7xA0PM24X0AU+VgU8dHLZMl13M"),
]


@pytest.mark.parametrize(("plaintext", "ciphertext"), VALID_VECTORS)
def test_openssl_vectors_preserve_every_padding_length_and_utf8(plaintext, ciphertext):
    assert decrypt_aes(ciphertext, PASSPHRASE) == plaintext


@pytest.mark.parametrize(("case", "ciphertext"), INVALID_PADDING)
def test_invalid_pkcs7_padding_is_rejected(case, ciphertext):
    with pytest.raises(ValueError):
        decrypt_aes(ciphertext, PASSPHRASE)


@pytest.mark.parametrize("length", range(32))
def test_truncated_openssl_envelope_is_rejected(length):
    raw = base64.b64decode(VALID_VECTORS[1][1])
    with pytest.raises(ValueError):
        decrypt_aes(base64.b64encode(raw[:length]).decode(), PASSPHRASE)


def test_existing_base64_line_wrapping_remains_supported():
    ciphertext = VALID_VECTORS[1][1]
    assert decrypt_aes(ciphertext[:24] + "\n" + ciphertext[24:], PASSPHRASE) == "x"


@pytest.mark.parametrize(("pin", "username", "expected"), [
    ("1234", "merchant", "1bc5aa3cc48f0e3cbf158b6c9730ee7b16df446ef5999423d103231d9042b091"),
    ("0000", "outlet@example.test", "1d295d256f31a0d92901c0c2db2c00a5891450e7f46865a67f2528cd41a605dc"),
    ("１２３４", "用户名", "b58bd037fd3279ec01fe1ac40e449c145c846ddffb06294bfcc448be12a62215"),
])
def test_existing_provider_pin_derivation_matches_node(pin, username, expected):
    assert encrypt_pin(pin, username) == expected
