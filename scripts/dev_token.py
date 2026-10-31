#!/usr/bin/env python3
"""Local-only token issuer that mimics Cognito access-token claims, for running the gateway
without Cognito. The gateway accepts these only with DEV_TOKENS_ENABLED=true and
ENVIRONMENT=local, and refuses to start otherwise.

    scripts/dev_token.py init                     # writes .dev/key.pem and .dev/jwks.json
    scripts/dev_token.py mint --user alice --team platform
    scripts/dev_token.py env                      # prints env vars for a local gateway
"""

import argparse
import json
import time
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

DEV = Path(__file__).resolve().parents[1] / ".dev"
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gateway"))
from app.config import DEV_CLIENT_ID as CLIENT_ID, DEV_ISSUER as ISSUER  # noqa: E402
KID = "dev-1"


def init() -> None:
    DEV.mkdir(exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (DEV / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                    serialization.NoEncryption()))
    (DEV / "key.pem").chmod(0o600)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    (DEV / "jwks.json").write_text(json.dumps({"keys": [{**jwk, "kid": KID, "alg": "RS256", "use": "sig"}]}))
    print(f"wrote {DEV}/key.pem and {DEV}/jwks.json")


def mint(user: str, team: str, ttl: int) -> str:
    key = serialization.load_pem_private_key((DEV / "key.pem").read_bytes(), password=None)
    now = int(time.time())
    claims = {"iss": ISSUER, "sub": f"dev-{user}", "username": user, "client_id": CLIENT_ID, "token_use": "access",
              "iat": now, "exp": now + ttl, "cognito:groups": [team]}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": KID})


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    m = sub.add_parser("mint")
    m.add_argument("--user", default="alice")
    m.add_argument("--team", default="platform")
    m.add_argument("--ttl", type=int, default=900)
    sub.add_parser("env")
    a = ap.parse_args()
    if a.cmd == "init":
        init()
    elif a.cmd == "mint":
        print(mint(a.user, a.team, a.ttl))
    else:
        print(f"export ENVIRONMENT=local DEV_TOKENS_ENABLED=true DEV_JWKS_PATH={DEV / 'jwks.json'} "
              f"BUDGET_BACKEND=memory POLICY_PATH={DEV.parent / 'gateway' / 'policy.yaml'}")


if __name__ == "__main__":
    main()
