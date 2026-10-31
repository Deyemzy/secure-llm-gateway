"""JWT re-verification. API Gateway already checked the token; the gateway checks it again
so that anything reaching the ALB from inside the VPC still needs a valid Cognito token."""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

import jwt

MAX_TOKEN_BYTES = 8192
# Team membership comes ONLY from Cognito groups, which users cannot change themselves (admin APIs
# only). Deliberately not configurable: pointing this at a user-writable attribute (custom:*,
# preferred_username, ...) would let users move themselves into a richer team.
TEAM_CLAIM = "cognito:groups"
ALLOWED_ALGS = frozenset({"RS256"})  # Cognito signs with RS256; never HS*/none


class AuthError(Exception):
    """Reason is for logs only; clients always get a generic 401."""


@dataclass(frozen=True)
class Principal:
    user: str        # username, for display, logs and per-user budget overrides
    sub: str         # stable subject id, for the budget ledger
    team: str
    client_id: str


class JWKSCache:
    """Signing keys from a static JWKS document (baked at deploy) and/or a URL refreshed on a TTL.

    An unknown kid triggers at most one refetch per `min_refresh_s`, so a flood of
    tokens with random kids cannot turn into a flood of JWKS fetches.
    """

    def __init__(self, static_jwks: dict | None = None, url: str | None = None, ttl_s: int = 3600,
                 min_refresh_s: int = 60, fetch: Callable[[str], dict] | None = None, clock=time.monotonic):
        if not static_jwks and not url:
            raise ValueError("need a JWKS document or URL")
        self.url, self.ttl_s, self.min_refresh_s = url, ttl_s, min_refresh_s
        self._fetch = fetch or _http_get_json
        self._clock = clock
        self._lock = threading.Lock()
        self._static = self._parse(static_jwks) if static_jwks else {}
        self._remote: dict[str, jwt.PyJWK] = {}
        self._fetched_at = float("-inf")

    @staticmethod
    def _parse(doc: dict) -> dict[str, jwt.PyJWK]:
        keys = {}
        for k in doc.get("keys", []):
            if k.get("kid") and k.get("kty") == "RSA" and k.get("use", "sig") == "sig":
                keys[k["kid"]] = jwt.PyJWK(k, algorithm="RS256")
        return keys

    def get(self, kid: str) -> jwt.PyJWK | None:
        if kid in self._static:
            return self._static[kid]
        if not self.url:
            return None
        with self._lock:
            age = self._clock() - self._fetched_at
            if age > self.ttl_s or (kid not in self._remote and age > self.min_refresh_s):
                try:
                    self._remote = self._parse(self._fetch(self.url))
                except Exception:  # keep serving cached keys if the IdP is unreachable
                    pass
                self._fetched_at = self._clock()
            return self._remote.get(kid)


def _http_get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=3) as r:  # noqa: S310 - fixed https URL from config
        return json.loads(r.read(1_000_000))


class JWTVerifier:
    def __init__(self, keys: JWKSCache, issuer: str, client_ids: set[str], required_scope: str | None = None,
                 max_lifetime_s: int = 900, leeway_s: int = 30):
        self.keys = keys
        self.issuer = issuer
        self.client_ids = client_ids
        self.required_scope = required_scope
        self.max_lifetime_s = max_lifetime_s
        self.leeway_s = leeway_s

    def verify(self, authorization: str | None) -> Principal:
        if not authorization or not authorization.startswith("Bearer "):
            raise AuthError("missing bearer token")
        token = authorization[7:].strip()
        if not token or len(token) > MAX_TOKEN_BYTES:
            raise AuthError("token empty or too large")

        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as e:
            raise AuthError(f"malformed header: {e}") from None
        alg, kid = header.get("alg"), header.get("kid")
        if alg not in ALLOWED_ALGS:
            raise AuthError(f"algorithm not allowed: {alg}")
        if not isinstance(kid, str) or not kid:
            raise AuthError("missing kid")
        key = self.keys.get(kid)
        if key is None:
            raise AuthError("unknown kid")

        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key.key,
                algorithms=["RS256"],
                issuer=self.issuer,
                leeway=self.leeway_s,
                options={"require": ["exp", "iat", "iss", "sub", "token_use"], "verify_aud": False},
            )
        except jwt.InvalidTokenError as e:
            raise AuthError(f"invalid token: {type(e).__name__}") from None

        # Cognito access tokens carry client_id instead of aud; ID tokens are not accepted here.
        if claims.get("token_use") != "access":
            raise AuthError("not an access token")
        client_id = claims.get("client_id")
        if client_id not in self.client_ids:
            raise AuthError("client_id not allowed")
        if claims["exp"] - claims["iat"] > self.max_lifetime_s:
            raise AuthError("token lifetime too long")
        if claims["iat"] > time.time() + self.leeway_s:
            raise AuthError("token issued in the future")
        if self.required_scope and self.required_scope not in str(claims.get("scope", "")).split():
            raise AuthError("missing scope")

        groups = claims.get(TEAM_CLAIM)
        if isinstance(groups, str):
            groups = [groups]
        if not isinstance(groups, list) or len(groups) != 1 or not isinstance(groups[0], str):
            raise AuthError("token must carry exactly one team")

        # Identity: `username` (immutable in Cognito) and `sub`. Never email, preferred_username or custom:*.
        user = claims.get("username") or claims["sub"]
        return Principal(user=str(user), sub=str(claims["sub"]), team=groups[0], client_id=str(client_id))
