"""JWT abuse cases: forged algorithms, wrong keys, wrong token types, missing team."""

import base64
import json

import jwt
import pytest

from app.auth import AuthError, JWKSCache, JWTVerifier
from conftest import CLIENT_ID, ISSUER, JWKS, _OTHER_KEY, jwk_of, mint


def b64(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


def rejects(verifier, header_value, why=None):
    with pytest.raises(AuthError) as e:
        verifier.verify(header_value)
    if why:
        assert why in str(e.value)


def test_valid_token(verifier):
    p = verifier.verify(f"Bearer {mint(user='alice', team='research')}")
    assert (p.user, p.team, p.client_id) == ("alice", "research", CLIENT_ID)


@pytest.mark.parametrize("value", [None, "", "Bearer", "Bearer ", "Basic abc", "bearer " + "x", "Bearer " + "a" * 9000])
def test_missing_or_malformed_header(verifier, value):
    rejects(verifier, value)


def test_alg_none_rejected(verifier):
    claims = jwt.decode(mint(), options={"verify_signature": False})
    token = f"{b64({'alg': 'none', 'kid': 'k1', 'typ': 'JWT'})}.{b64(claims)}."
    rejects(verifier, f"Bearer {token}", "algorithm not allowed")


def test_hs256_key_confusion_rejected(verifier):
    # Classic confusion: sign with HS256 using the RSA *public* key as the HMAC secret.
    pub = json.dumps(JWKS["keys"][0]).encode()
    claims = jwt.decode(mint(), options={"verify_signature": False})
    header = b64({"alg": "HS256", "kid": "k1", "typ": "JWT"})
    import hashlib, hmac
    body = f"{header}.{b64(claims)}"
    sig = base64.urlsafe_b64encode(hmac.new(pub, body.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    rejects(verifier, f"Bearer {body}.{sig}", "algorithm not allowed")


def test_signed_by_unknown_key_with_known_kid(verifier):
    rejects(verifier, f"Bearer {mint(key=_OTHER_KEY, kid='k1')}", "InvalidSignatureError")


def test_unknown_kid(verifier):
    rejects(verifier, f"Bearer {mint(key=_OTHER_KEY, kid='attacker')}", "unknown kid")


def test_tampered_payload(verifier):
    h, p, s = mint(team="research").split(".")
    claims = json.loads(base64.urlsafe_b64decode(p + "=="))
    claims["cognito:groups"] = ["platform"]
    rejects(verifier, f"Bearer {h}.{b64(claims)}.{s}", "InvalidSignatureError")


@pytest.mark.parametrize("overrides,why", [
    ({"exp": 1}, "ExpiredSignatureError"),
    ({"iss": "https://evil.example.com"}, "InvalidIssuerError"),
    ({"token_use": "id"}, "not an access token"),
    ({"client_id": "someone-else"}, "client_id not allowed"),
    ({"client_id": None}, "client_id not allowed"),
    ({"cognito:groups": None}, "exactly one team"),
    ({"cognito:groups": []}, "exactly one team"),
    ({"cognito:groups": ["platform", "research"]}, "exactly one team"),
    ({"token_use": None}, "MissingRequiredClaimError"),
])
def test_claim_checks(verifier, overrides, why):
    rejects(verifier, f"Bearer {mint(**overrides)}", why)


def test_overlong_lifetime_rejected(verifier):
    rejects(verifier, f"Bearer {mint(ttl=86400)}", "lifetime too long")


def test_future_iat_rejected(verifier):
    import time
    now = int(time.time())
    rejects(verifier, f"Bearer {mint(iat=now + 3600, exp=now + 3700)}")


def test_required_scope(verifier):
    v = JWTVerifier(verifier.keys, ISSUER, {CLIENT_ID}, required_scope="llm-gateway/invoke")
    rejects(v, f"Bearer {mint()}", "missing scope")
    assert v.verify(f"Bearer {mint(scope='openid llm-gateway/invoke')}").team == "platform"


def test_jwks_refetch_is_rate_limited():
    fetches = []
    t = [0.0]

    def fetch(url):
        fetches.append(url)
        return {"keys": [jwk_of(_OTHER_KEY, "rotated")]}

    cache = JWKSCache(url="https://idp/jwks", fetch=fetch, clock=lambda: t[0])
    assert cache.get("rotated") is not None
    for i in range(100):  # flood of random kids
        assert cache.get(f"bogus-{i}") is None
    assert len(fetches) == 1
    t[0] += 61
    cache.get("bogus-again")
    assert len(fetches) == 2


def test_jwks_fetch_failure_keeps_cached_keys():
    t = [0.0]
    calls = {"n": 0}

    def fetch(url):
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("idp down")
        return {"keys": [jwk_of(_OTHER_KEY, "k")]}

    cache = JWKSCache(url="https://idp/jwks", fetch=fetch, clock=lambda: t[0])
    assert cache.get("k") is not None
    t[0] += 7200
    assert cache.get("k") is not None


# ---- team comes from cognito:groups only -------------------------------------------------

# Attributes a user can edit about themselves through Cognito (UpdateUserAttributes), plus
# claims an attacker might hope the gateway trusts.
SELF_EDITED = {"custom:team": "platform", "custom:cognito:groups": ["platform"], "team": "platform",
               "groups": ["platform"], "preferred_username": "admin", "email": "ceo@example.com",
               "name": "platform", "custom:role": "admin"}


def test_self_edited_attributes_do_not_change_team_or_identity(verifier):
    p = verifier.verify(f"Bearer {mint(user='carol', team='research', extra_claims=SELF_EDITED)}")
    assert (p.team, p.user, p.sub) == ("research", "carol", "sub-carol")


def test_custom_attribute_alone_grants_no_team(verifier):
    rejects(verifier, f"Bearer {mint(extra_claims={**SELF_EDITED, 'cognito:groups': None})}", "exactly one team")


def test_default_lifetime_matches_15_minute_tokens(verifier):
    assert verifier.verify(f"Bearer {mint(ttl=900)}").team == "platform"
    rejects(verifier, f"Bearer {mint(ttl=901)}", "lifetime too long")
