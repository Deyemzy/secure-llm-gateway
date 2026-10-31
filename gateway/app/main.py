"""OpenAI-compatible request pipeline. Each step either passes or ends the request:

  1. verify JWT, read user + team          401 unauthorized
  2. validate request shape                 400 invalid_request / unsupported_parameter / unknown_parameter
  3. resolve model alias against policy     403 model_not_allowed / team_not_allowed
  4. reserve budget (Redis Lua)             429 budget_exceeded / rate_limited, 409 duplicate_request
  5. Converse + Guardrail                   400 blocked_by_guardrail, 429 upstream_throttled
  6. settle actual cost, log metadata
  7. return                                 200 (+ X-Budget-Remaining-USD, X-Request-Id)

Dependency outages (Redis, Bedrock) fail closed with 503. Errors use the OpenAI error shape so
stock SDKs surface them, and carry x-should-retry so SDKs don't retry what can't succeed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from .auth import AuthError, JWKSCache, JWTVerifier, Principal
from .bedrock import (Bedrock, UpstreamOutcomeUnknown, UpstreamRejected, UpstreamThrottled, UpstreamUnavailable,
                      make_client)
from .budget import IDEMPOTENCY_KEY, BudgetStore, BudgetUnavailable, InMemoryBudget, RedisBudget, request_id_for
from .config import Settings, StartupError
from .logging import AuditLog
from .policy import (ConfigError, Limits, PolicyDenied, PolicyStore, PolicyUnavailable, file_source,
                     input_token_upper_bound, ssm_source)
from .schema import MAX_BODY_BYTES, RequestRejected, parse

ERROR_TYPES = {400: "invalid_request_error", 401: "authentication_error", 403: "permission_error",
               404: "not_found_error", 405: "invalid_request_error", 409: "conflict_error", 429: "rate_limit_error"}
RETRYABLE = {"rate_limited", "upstream_throttled", "upstream_unavailable", "dependency_unavailable",
             "policy_unavailable", "reservation_expired"}
log_ = logging.getLogger("gateway")


class GatewayError(Exception):
    def __init__(self, status: int, code: str, message: str, param: str | None = None,
                 retry_after: int | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.param, self.retry_after = status, code, message, param, retry_after


@dataclass
class Deps:
    verifier: JWTVerifier
    policy: PolicyStore
    budget: BudgetStore
    bedrock: Bedrock
    audit: AuditLog
    reconcile_interval_s: float | None = None   # None: no background reconciler (tests drive it)


def usd(micro: int | None) -> float | None:
    return None if micro is None else round(micro / 1e6, 6)


def remaining_usd(limits: Limits, team_spent: int, user_spent: int) -> str:
    left = max(0, min(limits.team_micro_usd - team_spent, limits.user_micro_usd - user_spent))
    return f"{left / 1e6:.6f}"


def client_ip(request: Request) -> str:
    """For logs only, never for decisions. API Gateway overwrites X-Client-Ip with the caller's
    address; otherwise the ALB appends the real peer as the last X-Forwarded-For entry."""
    if ip := request.headers.get("x-client-ip"):
        return ip
    xff = request.headers.get("x-forwarded-for", "")
    return xff.split(",")[-1].strip() if xff else (request.client.host if request.client else "")


def error_response(err: GatewayError, extra_headers: dict | None = None) -> JSONResponse:
    headers = {"x-should-retry": "true" if err.code in RETRYABLE else "false", **(extra_headers or {})}
    if err.retry_after:
        headers["Retry-After"] = str(err.retry_after)
    body = {"error": {"message": err.message, "type": ERROR_TYPES.get(err.status, "api_error"),
                      "code": err.code, "param": err.param}}
    return JSONResponse(body, status_code=err.status, headers=headers)


def run_reconcile(deps: Deps) -> list:
    """Recover holds whose worker died. Safe to run concurrently from every task."""
    outcomes = deps.budget.reconcile()
    for o in outcomes:
        if o.outcome in ("expired_released", "expired_charged"):
            deps.audit.event("reconcile", ledger_id=o.request_id, outcome=o.outcome, team=o.team, sub=o.user,
                             reserved_usd=usd(o.amount))
    return outcomes


def create_app(deps: Deps) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if deps.reconcile_interval_s:
            async def loop():
                while True:
                    await asyncio.sleep(deps.reconcile_interval_s)
                    try:
                        await run_in_threadpool(run_reconcile, deps)
                    except Exception as e:  # never let the reconciler take the process down
                        log_.error("reconcile failed: %s", type(e).__name__)
            task = asyncio.create_task(loop())
        yield
        if task:
            task.cancel()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.middleware("http")
    async def headers(request: Request, call_next):
        request.state.rid = uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.rid
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):
        # A bug or crash mid-request. Any budget hold is deliberately left open: we can't know whether
        # Bedrock billed, so the reconciler closes it later (refund if never invoked, else keep charged).
        log_.error("unhandled %s on %s", type(exc).__name__, request.url.path)
        resp = error_response(GatewayError(500, "internal_error", "internal error"))
        resp.headers["X-Request-Id"] = getattr(request.state, "rid", "")
        return resp

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "error")
        return error_response(GatewayError(exc.status_code, code, code.replace("_", " ")))

    def fail(err: GatewayError, log: dict, headers: dict | None = None) -> JSONResponse:
        deps.audit.request(**log, status=err.status, code=err.code,
                           outcome="error" if err.status >= 500 else "block")
        return error_response(err, headers)

    def authenticate(request: Request, log: dict) -> Principal:
        try:
            p = deps.verifier.verify(request.headers.get("authorization"))
        except AuthError as e:
            log["detail"] = str(e)
            raise GatewayError(401, "unauthorized", "missing or invalid token") from None
        log.update(user=p.user, sub=p.sub, team=p.team, client_id=p.client_id)
        return p

    def current_policy():
        try:
            return deps.policy.current()
        except PolicyUnavailable:
            raise GatewayError(503, "policy_unavailable", "gateway policy unavailable; try again later",
                               retry_after=30) from None

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/v1/models")
    async def models(request: Request):
        log = {"request_id": request.state.rid, "method": "GET", "path": "/v1/models", "source_ip": client_ip(request)}
        try:
            p = authenticate(request, log)
            names = current_policy().allowed_models(p.team)
        except GatewayError as e:
            return fail(e, log)
        except PolicyDenied as e:
            return fail(GatewayError(e.status, e.code, e.message, e.param), log)
        deps.audit.request(**log, status=200, outcome="allow")
        return {"object": "list", "data": [{"id": n, "object": "model", "created": 0, "owned_by": "gateway"}
                                           for n in names]}

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        started = time.monotonic()
        rid = request.state.rid
        log: dict = {"request_id": rid, "method": "POST", "path": "/v1/chat/completions",
                     "source_ip": client_ip(request)}
        reservation = None
        limits: Limits | None = None
        budget_hdr: dict = {}
        try:
            # 1. identity
            principal = authenticate(request, log)

            # 2. shape
            raw = await request.body()
            if len(raw) > MAX_BODY_BYTES:
                raise GatewayError(400, "request_too_large", f"body exceeds {MAX_BODY_BYTES} bytes")
            try:
                req = parse(json.loads(raw))
            except ValueError:
                raise GatewayError(400, "invalid_request", "body must be valid JSON") from None
            except RequestRejected as e:
                raise GatewayError(400, e.code, e.message, e.param) from None
            log["model"] = req.model

            idem = request.headers.get("idempotency-key")
            if idem is not None and not IDEMPOTENCY_KEY.match(idem):
                raise GatewayError(400, "invalid_request", "Idempotency-Key must be 8-128 chars of [A-Za-z0-9._:-]",
                                   param="Idempotency-Key")

            # 3. policy (one snapshot for the whole request)
            policy = current_policy()
            try:
                route = policy.resolve(principal.team, req.model, req.max_tokens, req.sampling)
                limits = policy.limits(principal.team, principal.user)
                screened = policy.screen_input([m.text for m in req.messages])
            except PolicyDenied as e:
                raise GatewayError(e.status, e.code, e.message, e.param) from None
            log.update(model_id=route.model_id, input_redactions=screened.redactions or None)
            texts = iter(screened.texts)
            cleaned = [(m.role, next(texts)) for m in req.messages]
            system = [t for role, t in cleaned if role == "system"]
            turns = [(role, t) for role, t in cleaned if role != "system"]

            # 4. budget: reserve -> begin -> replicated, all before any money is spent
            worst = route.cost_micro_usd(input_token_upper_bound(screened.texts), req.max_tokens)
            ledger_id = request_id_for(principal.sub, idem) if idem else rid
            log["ledger_id"] = ledger_id
            try:
                res = await run_in_threadpool(deps.budget.reserve, principal.team, principal.sub, worst,
                                              limits.team_micro_usd, limits.user_micro_usd, limits.user_rpm,
                                              ledger_id)
            except BudgetUnavailable:
                raise GatewayError(503, "dependency_unavailable", "budget service unavailable; try again",
                                   retry_after=5) from None
            budget_hdr = {"X-Budget-Remaining-USD": remaining_usd(limits, res.team_spent, res.user_spent)}
            if not res.ok:
                if res.reason == "rate_limited":
                    raise GatewayError(429, "rate_limited", "too many requests; slow down", retry_after=60)
                if res.reason == "duplicate_in_flight":
                    raise GatewayError(409, "duplicate_request", "a request with this Idempotency-Key is still "
                                       "in progress", param="Idempotency-Key")
                if res.reason == "duplicate_completed":
                    raise GatewayError(409, "already_completed", "a request with this Idempotency-Key already "
                                       "completed; responses are not stored, so it was not run again",
                                       param="Idempotency-Key")
                scope = "team" if res.reason.startswith("team") else "user"
                raise GatewayError(429, "budget_exceeded", f"monthly {scope} budget exhausted "
                                   f"(this request could cost up to ${worst / 1e6:.4f})")
            reservation = res.reservation
            log["reserved_usd"] = usd(worst)
            try:
                started_ok = await run_in_threadpool(deps.budget.begin, reservation)
                replicated = started_ok and await run_in_threadpool(deps.budget.wait_replicated)
            except BudgetUnavailable:
                raise GatewayError(503, "dependency_unavailable", "budget service unavailable; try again",
                                   retry_after=5) from None
            if not started_ok:
                raise GatewayError(503, "reservation_expired", "budget hold expired before the model call; retry",
                                   retry_after=1)
            if not replicated:
                log["detail"] = "replica_ack_timeout"
                raise GatewayError(503, "dependency_unavailable", "budget hold not durable; try again",
                                   retry_after=5)

            # 5. model
            t0 = time.monotonic()
            try:
                result = await run_in_threadpool(deps.bedrock.converse, route.model_id, system, turns,
                                                 req.max_tokens, req.stop_sequences, req.sampling,
                                                 {"gateway_request_id": rid, "ledger_id": ledger_id})
            except UpstreamThrottled as e:
                raise GatewayError(429, "upstream_throttled", "model capacity exhausted; retry later",
                                   retry_after=e.retry_after) from None
            except UpstreamUnavailable:
                raise GatewayError(503, "upstream_unavailable", "model temporarily unavailable",
                                   retry_after=5) from None
            except UpstreamRejected as e:
                log["detail"] = e.code
                raise GatewayError(502, "upstream_error", "model call failed") from None
            except UpstreamOutcomeUnknown as e:
                # Bedrock may have billed and we lost the usage: charge the hold (a proven ceiling on cost).
                # Never guess zero for an unknown outcome.
                settled = await _settle(deps, reservation, reservation.amount, log, "charged_unknown")
                reservation = None
                if settled:
                    budget_hdr = {"X-Budget-Remaining-USD": remaining_usd(limits, settled.team_spent,
                                                                          settled.user_spent)}
                log.update(detail=e.code, cost_usd=usd(res.reservation.amount), outcome_unknown=True)
                raise GatewayError(502, "upstream_outcome_unknown",
                                   f"the model call's outcome is unknown, so it was charged at its maximum "
                                   f"(${res.reservation.amount / 1e6:.4f}); you may retry") from None
            log["upstream_ms"] = round((time.monotonic() - t0) * 1000)

            # 6. settle (actual usage, even when the guardrail blocked: Bedrock bills it)
            actual = route.cost_micro_usd(result.input_tokens, result.output_tokens)
            settled = await _settle(deps, reservation, actual, log, "settled")
            reservation = None
            if settled:
                budget_hdr = {"X-Budget-Remaining-USD": remaining_usd(limits, settled.team_spent, settled.user_spent)}
            log.update(input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                       cost_usd=usd(actual), stop_reason=result.stop_reason)

            if result.guardrail_blocked:
                raise GatewayError(400, "blocked_by_guardrail", "request or response blocked by content policy")

            text, out_redactions = policy.screen_output(result.text)
            log.update(output_redactions=out_redactions or None, output_redacted=bool(out_redactions))
        except GatewayError as e:
            if reservation is not None:  # never reached the model, or it failed unbilled: release the hold
                settled = await _settle(deps, reservation, 0, log, "released")
                if settled and limits:
                    budget_hdr = {"X-Budget-Remaining-USD": remaining_usd(limits, settled.team_spent,
                                                                          settled.user_spent)}
            log["latency_ms"] = round((time.monotonic() - started) * 1000)
            return fail(e, log, budget_hdr)

        log["latency_ms"] = round((time.monotonic() - started) * 1000)
        deps.audit.request(**log, status=200, outcome="allow")
        finish = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length"}.get(result.stop_reason, "stop")
        body = {
            "id": f"chatcmpl-{rid}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": req.model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": finish,
                         "logprobs": None}],
            "usage": {"prompt_tokens": result.input_tokens, "completion_tokens": result.output_tokens,
                      "total_tokens": result.input_tokens + result.output_tokens},
        }
        return JSONResponse(body, headers=budget_hdr)

    return app


async def _settle(deps: Deps, reservation, actual: int, log: dict, state: str):
    try:
        return await run_in_threadpool(deps.budget.settle, reservation, actual, state)
    except BudgetUnavailable:
        # The hold stays open; the reconciler closes it later. Over-counts, never under-counts.
        log["detail"] = "settle_failed"
        return None


def deps_from_settings(s: Settings) -> Deps:
    import boto3

    verifier = JWTVerifier(JWKSCache(static_jwks=s.jwks, url=s.jwks_url), issuer=s.jwt_issuer,
                           client_ids=set(s.jwt_client_ids), required_scope=s.required_scope,
                           max_lifetime_s=s.jwt_max_lifetime_s)

    if s.policy_ssm_parameter:
        source = ssm_source(s.policy_ssm_parameter, boto3.client("ssm", region_name=s.aws_region))
    else:
        source = file_source(s.policy_path)
    audit = AuditLog()
    # raises ConfigError (the app does not start): missing/invalid policy or unpriced model
    policy = PolicyStore(source, refresh_s=s.policy_refresh_s, max_stale_s=s.policy_max_stale_s,
                         on_reject=lambda why: audit.event("policy_refresh", outcome="rejected", detail=why))

    if s.budget_backend == "memory":
        budget: BudgetStore = InMemoryBudget()
    else:
        import redis
        client = redis.Redis(**s.redis, socket_timeout=1.0, socket_connect_timeout=1.0)
        budget = RedisBudget(client, min_replica_acks=s.redis_min_replica_acks)

    bedrock = Bedrock(make_client(s.aws_region), s.guardrail_id, s.guardrail_version)
    return Deps(verifier, policy, budget, bedrock, audit, reconcile_interval_s=s.reconcile_interval_s)


def build() -> FastAPI:
    """uvicorn entrypoint. Any configuration problem stops the process before it binds a port."""
    try:
        return create_app(deps_from_settings(Settings.from_env()))
    except (StartupError, ConfigError) as e:
        raise SystemExit(f"refusing to start: {e}") from None
