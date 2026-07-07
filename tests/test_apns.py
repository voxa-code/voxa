# tests/test_apns.py
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from server.apns import build_apns_jwt, build_voip_payload, build_cancel_payload


# Generate a throwaway P-256 key at runtime (never a real APNs key, and nothing
# key-shaped is committed to the repo, so secret scanners stay clean).
def _test_p8() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


TEST_P8 = _test_p8()

def test_voip_payload_shape():
    p = build_voip_payload("abc", "Claude finished")
    assert p["call_id"] == "abc"
    assert p["summary"] == "Claude finished"
    assert p["aps"]["content-available"] == 1

def test_cancel_payload_shape():
    p = build_cancel_payload("call-7")
    assert p["call_id"] == "call-7"
    assert p["type"] == "cancel"
    assert p["aps"]["content-available"] == 1
    assert "summary" not in p


def test_jwt_roundtrips_es256():
    token = build_apns_jwt(TEST_P8, "KEY123", "TEAM45", issued_at=1000)
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "ES256"
    assert header["kid"] == "KEY123"
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["iss"] == "TEAM45"
    assert claims["iat"] == 1000


def test_voip_payload_includes_approval_only_when_present():
    assert "approval" not in build_voip_payload("c1", "s")
    a = {"approval_id": "abc"}
    assert build_voip_payload("c1", "s", approval=a)["approval"] == a
