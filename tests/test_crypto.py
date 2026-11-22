import os
import re
import shutil
import subprocess

import pytest

from helios import crypto


def test_generator_is_on_curve():
    assert crypto.on_curve((crypto.BASE_X, crypto.BASE_Y))


def test_order_annihilates_generator():
    assert crypto.scalar_mult(crypto.ORDER, (crypto.BASE_X, crypto.BASE_Y)) == crypto.INFINITY


def test_shared_secret_agrees():
    a, b = crypto.generate_private_key(), crypto.generate_private_key()
    assert crypto.shared_secret(a, crypto.public_key(b)) == crypto.shared_secret(b, crypto.public_key(a))


def openssl(*args: str, cwd) -> str:
    return subprocess.run(["openssl", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


def field(text: str, label: str) -> bytes:
    """One hex block of `openssl ec -text`, as bytes."""
    block = re.search(label + r":\s*\n((?:\s+[0-9a-f:]+\n)+)", text)
    assert block is not None, label
    return bytes.fromhex(re.sub(r"[\s:]", "", block.group(1)))


def to_strap(point: bytes) -> bytes:
    """An OpenSSL point (04, x, y, each 21 bytes big endian) as the strap sends one."""
    assert point[0] == 4 and len(point) == 43
    x, y = int.from_bytes(point[1:22], "big"), int.from_bytes(point[22:], "big")
    return x.to_bytes(24, "little") + y.to_bytes(24, "little")


def test_matches_openssl_sect163r2(tmp_path):
    """The key exchange against OpenSSL's, with OpenSSL's own keys: the
    public key Helios derives and the secret the two sides arrive at."""
    if shutil.which("openssl") is None:
        pytest.skip("no openssl command")
    try:
        for name in ("a", "b"):
            openssl("ecparam", "-name", "sect163r2", "-genkey", "-noout", "-out", f"{name}.pem", cwd=tmp_path)
    except subprocess.CalledProcessError as error:
        if os.environ.get("CI"):
            raise  # CI's OpenSSL has the curve; never pass there by skipping
        pytest.skip(f"this OpenSSL has no sect163r2: {error.stderr.strip()[:80]}")
    openssl("ec", "-in", "b.pem", "-pubout", "-out", "b.pub", cwd=tmp_path)
    openssl("pkeyutl", "-derive", "-inkey", "a.pem", "-peerkey", "b.pub", "-out", "shared.bin", cwd=tmp_path)
    ours = openssl("ec", "-in", "a.pem", "-text", "-noout", cwd=tmp_path)
    theirs = openssl("ec", "-in", "b.pub", "-pubin", "-text", "-noout", cwd=tmp_path)
    private = int.from_bytes(field(ours, "priv"), "big")
    assert crypto.public_key(private) == to_strap(field(ours, "pub"))
    secret = crypto.shared_secret(private, to_strap(field(theirs, "pub")))
    # OpenSSL gives the shared x coordinate, 21 bytes, big endian.
    assert secret[:21] == (tmp_path / "shared.bin").read_bytes()[::-1]


def test_the_point_of_order_two_is_refused():
    # (0, sqrt(b)) is on the curve, and doubles to infinity: sent as a public
    # key it would make the shared secret the same for every private key.
    root = crypto.COEFF_B
    for _ in range(crypto.DEGREE - 1):
        root = crypto._mul(root, root)
    weak = (0, root)
    assert crypto.on_curve(weak)
    with pytest.raises(ValueError, match="degenerate"):
        crypto.shared_secret(crypto.generate_private_key(), crypto.encode_point(weak))


def test_rejects_point_off_curve():
    with pytest.raises(ValueError):
        crypto.shared_secret(crypto.generate_private_key(), bytes(47) + b"\x01")


# FIPS 197 appendix C.1, and SP 800-38A F.1.1 (ECB) and F.2.1 (CBC).
FIPS_KEY = bytes(range(16))
SP_KEY = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
SP_PLAIN = bytes.fromhex(
    "6bc1bee22e409f96e93d7e117393172a"
    "ae2d8a571e03ac9c9eb76fac45af8e51"
    "30c81c46a35ce411e5fbc1191a0a52ef"
    "f69f2445df4f9b17ad2b417be66c3710"
)
SP_ECB = bytes.fromhex(
    "3ad77bb40d7a3660a89ecaf32466ef97"
    "f5d3d58503b9699de785895a96fdbaaf"
    "43b1cd7f598ece23881b00e3ed030688"
    "7b0c785e27e8ad3f8223207104725dd4"
)
SP_CBC = bytes.fromhex(
    "7649abac8119b246cee98e9b12e9197d"
    "5086cb9b507219ee95db113a917678b2"
    "73bed6b8e3c1743b7116e69e22229516"
    "3ff1caa1681fac09120eca307586e1a7"
)


def test_aes_matches_the_published_vectors():
    plain = bytes.fromhex("00112233445566778899aabbccddeeff")
    cipher = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
    assert crypto.aes_ecb_encrypt(plain, FIPS_KEY) == cipher
    assert crypto.aes_ecb_decrypt(cipher, FIPS_KEY) == plain
    assert crypto.aes_ecb_encrypt(SP_PLAIN, SP_KEY) == SP_ECB
    assert crypto.aes_ecb_decrypt(SP_ECB, SP_KEY) == SP_PLAIN
    # CBC here always pads, so the standard's four blocks are followed by a fifth.
    assert crypto.aes_cbc_encrypt(SP_PLAIN, SP_KEY, bytes(range(16)))[:64] == SP_CBC


def test_the_s_box_is_the_standards():
    assert crypto._SBOX[:8].hex() == "637c777bf26b6fc5" and crypto._SBOX[0x53] == 0xED
    assert sorted(crypto._SBOX) == list(range(256))
    assert all(crypto._INV_SBOX[crypto._SBOX[b]] == b for b in range(256))


def test_aes_matches_openssl_on_random_input():
    ciphers = pytest.importorskip("cryptography.hazmat.primitives.ciphers")
    padding = pytest.importorskip("cryptography.hazmat.primitives.padding")
    import secrets

    for blocks in (1, 2, 3, 7, 16, 65):
        key, iv, data = secrets.token_bytes(16), secrets.token_bytes(16), secrets.token_bytes(16 * blocks)
        ecb = ciphers.Cipher(ciphers.algorithms.AES(key), ciphers.modes.ECB())
        encrypted = ecb.encryptor().update(data)
        assert crypto.aes_ecb_encrypt(data, key) == encrypted
        assert crypto.aes_ecb_decrypt(encrypted, key) == data
        assert ecb.decryptor().update(crypto.aes_ecb_encrypt(data, key)) == data
        # CBC with PKCS#7, on lengths that do and don't fill a block.
        for cut in (0, 1, 15):
            message = data[: len(data) - cut]
            padder = padding.PKCS7(128).padder()
            cbc = ciphers.Cipher(ciphers.algorithms.AES(key), ciphers.modes.CBC(iv)).encryptor()
            expected = cbc.update(padder.update(message) + padder.finalize()) + cbc.finalize()
            assert crypto.aes_cbc_encrypt(message, key, iv) == expected


@pytest.mark.parametrize("call", [crypto.aes_ecb_encrypt, crypto.aes_ecb_decrypt])
def test_aes_refuses_ragged_input(call):
    with pytest.raises(ValueError):
        call(bytes(15), bytes(16))
    with pytest.raises(ValueError):
        call(bytes(16), bytes(24))
    assert call(b"", bytes(16)) == b""
