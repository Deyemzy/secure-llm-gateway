"""Startup configuration and fail-closed checks. Anything unsafe stops the process before it
can serve a request."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEV_ISSUER = "https://local.dev/slg"
DEV_CLIENT_ID = "local-dev"


class StartupError(Exception):
    pass


def _bool(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    environment: str
    dev_tokens: bool
    jwt_issuer: str
    jwt_client_ids: frozenset[str]
    jwks: dict | None
    jwks_url: str | None
    jwt_max_lifetime_s: int
    required_scope: str | None
    policy_ssm_parameter: str | None
    policy_path: str | None
    budget_backend: str          # "redis" | "memory"
    redis: dict
    redis_min_replica_acks: int
    reconcile_interval_s: float
    policy_refresh_s: float
    policy_max_stale_s: float
    aws_region: str
    guardrail_id: str | None
    guardrail_version: str | None

    @property
    def is_local(self) -> bool:
        return self.environment == "local"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = dict(os.environ if env is None else env)
        environment = env.get("ENVIRONMENT", "").strip().lower()
        if not environment:
            raise StartupError("ENVIRONMENT must be set (local, dev, prod, ...)")
        local = environment == "local"

        # Dev-signed tokens: off unless explicitly enabled, and never outside local.
        dev_tokens = _bool(env.get("DEV_TOKENS_ENABLED"))
        if dev_tokens and not local:
            raise StartupError(f"DEV_TOKENS_ENABLED is set but ENVIRONMENT={environment}; dev tokens are local-only")

        if dev_tokens:
            jwks_path = Path(env.get("DEV_JWKS_PATH", ".dev/jwks.json"))
            try:
                jwks = json.loads(jwks_path.read_text())
            except (OSError, ValueError) as e:
                raise StartupError(f"dev tokens enabled but {jwks_path} unreadable: {e}") from None
            issuer, client_ids, jwks_url = DEV_ISSUER, frozenset({DEV_CLIENT_ID}), None
        else:
            issuer = env.get("JWT_ISSUER", "")
            client_ids = frozenset(c for c in env.get("JWT_CLIENT_IDS", "").split(",") if c)
            jwks = json.loads(env["JWKS_JSON"]) if env.get("JWKS_JSON") else None
            jwks_url = env.get("JWKS_URL") or None
            if not issuer.startswith("https://") or not client_ids or not (jwks or jwks_url):
                raise StartupError("JWT_ISSUER (https), JWT_CLIENT_IDS and JWKS_JSON or JWKS_URL are required")
            if issuer == DEV_ISSUER:
                raise StartupError("JWT_ISSUER is the dev issuer; use DEV_TOKENS_ENABLED in a local environment")

        if "TEAM_CLAIM" in env:
            raise StartupError("TEAM_CLAIM is not configurable: team always comes from cognito:groups")

        budget_backend = env.get("BUDGET_BACKEND", "redis")
        if budget_backend not in ("redis", "memory"):
            raise StartupError(f"unknown BUDGET_BACKEND {budget_backend}")
        if budget_backend == "memory" and not local:
            raise StartupError("BUDGET_BACKEND=memory is local-only")
        if budget_backend == "redis" and not env.get("REDIS_HOST"):
            raise StartupError("REDIS_HOST is required")

        if not local and not env.get("BEDROCK_GUARDRAIL_ID", "").startswith("arn:aws:bedrock:"):
            raise StartupError("BEDROCK_GUARDRAIL_ID must be the guardrail ARN outside local "
                               "(the IAM deny condition matches on the ARN)")

        ssm_param = env.get("POLICY_SSM_PARAMETER") or None
        policy_path = env.get("POLICY_PATH") or None
        if not ssm_param and not (local and policy_path):
            raise StartupError("POLICY_SSM_PARAMETER is required (POLICY_PATH is accepted only when ENVIRONMENT=local)")

        return cls(
            environment=environment,
            dev_tokens=dev_tokens,
            jwt_issuer=issuer,
            jwt_client_ids=client_ids,
            jwks=jwks,
            jwks_url=jwks_url,
            jwt_max_lifetime_s=int(env.get("JWT_MAX_LIFETIME_S", "900")),
            required_scope=env.get("JWT_REQUIRED_SCOPE") or None,
            policy_ssm_parameter=ssm_param,
            policy_path=policy_path,
            budget_backend=budget_backend,
            redis=dict(host=env.get("REDIS_HOST", ""), port=int(env.get("REDIS_PORT", "6379")),
                       username=env.get("REDIS_USER") or None, password=env.get("REDIS_PASSWORD") or None,
                       ssl=_bool(env.get("REDIS_TLS", "true"))),
            redis_min_replica_acks=int(env.get("REDIS_MIN_REPLICA_ACKS", "0")),
            reconcile_interval_s=float(env.get("RECONCILE_INTERVAL_S", "30")),
            policy_refresh_s=float(env.get("POLICY_REFRESH_S", "60")),
            policy_max_stale_s=float(env.get("POLICY_MAX_STALE_S", "900")),  # = token lifetime: 15-min revocation
            aws_region=env.get("AWS_REGION", "us-east-1"),
            guardrail_id=env.get("BEDROCK_GUARDRAIL_ID") or None,
            guardrail_version=env.get("BEDROCK_GUARDRAIL_VERSION") or None,
        )
