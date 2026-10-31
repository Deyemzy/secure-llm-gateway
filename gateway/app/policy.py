"""Policy: model aliases, prices, team allowlists, monthly budgets, screening patterns.

Loaded from SSM Parameter Store (the contents of gateway/policy.yaml, published by Terraform) and
validated strictly. There is no cryptographic signing: integrity rests on IAM (only the deployer can
write the parameter; the task can only read it). Fail-closed rules:

  - first load missing or invalid               -> the process refuses to start
  - a later refresh missing or invalid          -> keep serving the last validated policy,
                                                   for at most max_stale_s (default 900 s)
  - no successful refresh for max_stale_s       -> every request gets 503 policy_unavailable
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = logging.getLogger("gateway.policy")

ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)
MESSAGE_OVERHEAD_TOKENS = 16
NAME = r"^[a-z0-9][a-z0-9-]{0,31}$"


class ConfigError(Exception):
    """Policy is missing or invalid. Never serve traffic with it."""


class PolicyUnavailable(Exception):
    """No validated policy fresh enough to serve with. Every request fails closed (503)."""


class PolicyDenied(Exception):
    def __init__(self, code: str, message: str, status: int = 403, param: str | None = None):
        super().__init__(message)
        self.code, self.message, self.status, self.param = code, message, status, param


# ---- config schema -------------------------------------------------------------

class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelCfg(_Cfg):
    model_id: str = Field(min_length=3)
    input_usd_per_mtok: float | None = None   # presence enforced by check_prices()
    output_usd_per_mtok: float | None = None
    max_tokens: int = Field(ge=1, le=8192)
    sampling: list[Literal["temperature", "top_p"]] = []
    sampling_exclusive: bool = False           # model rejects temperature and top_p together


class TeamCfg(_Cfg):
    models: list[str] = Field(min_length=1)
    user_requests_per_minute: int = Field(ge=1, le=10_000)


class TeamBudgetCfg(_Cfg):
    team_monthly_usd: float | None = Field(default=None, ge=0)
    user_monthly_usd: float | None = Field(default=None, ge=0)


class DefaultBudgetCfg(_Cfg):
    team_monthly_usd: float = Field(ge=0)
    user_monthly_usd: float = Field(ge=0)


class UserBudgetCfg(_Cfg):
    monthly_usd: float = Field(ge=0)


class BudgetsCfg(_Cfg):
    default: DefaultBudgetCfg
    teams: dict[str, TeamBudgetCfg] = {}
    users: dict[str, UserBudgetCfg] = {}


class ScreeningCfg(_Cfg):
    secrets: dict[str, str] = {}
    pii: dict[str, str] = {}


class PolicyCfg(_Cfg):
    models: dict[str, ModelCfg] = Field(min_length=1)
    teams: dict[str, TeamCfg] = Field(min_length=1)
    budgets: BudgetsCfg
    screening: ScreeningCfg = ScreeningCfg()


# ---- runtime types -------------------------------------------------------------

@dataclass(frozen=True)
class Route:
    alias: str
    model_id: str
    input_usd_per_mtok: float
    output_usd_per_mtok: float
    max_tokens: int
    sampling: frozenset[str] = frozenset()
    sampling_exclusive: bool = False

    def cost_micro_usd(self, input_tokens: int, output_tokens: int) -> int:
        # USD per million tokens == micro-USD per token.
        return math.ceil(input_tokens * self.input_usd_per_mtok + output_tokens * self.output_usd_per_mtok)


@dataclass(frozen=True)
class Limits:
    team_micro_usd: int
    user_micro_usd: int
    user_rpm: int


@dataclass
class Screened:
    texts: list[str]
    redactions: dict[str, int] = field(default_factory=dict)


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(ZERO_WIDTH)


def luhn_ok(s: str) -> bool:
    nums = [int(c) for c in s if c.isdigit()]
    if not 13 <= len(nums) <= 19:
        return False
    total = 0
    for i, n in enumerate(reversed(nums)):
        if i % 2:
            n = n * 2 - 9 if n * 2 > 9 else n * 2
        total += n
    return total % 10 == 0


def input_token_upper_bound(texts: list[str]) -> int:
    """Conservative: no tokenizer emits more tokens than UTF-8 bytes, plus per-message framing."""
    return sum(len(t.encode()) + MESSAGE_OVERHEAD_TOKENS for t in texts)


def usd_to_micro(usd: float) -> int:
    return round(usd * 1_000_000)


def check_prices(cfg: PolicyCfg) -> None:
    """Startup gate: every model any team can use must have a positive input and output price,
    otherwise the budget reservation for it would be zero and spend would be uncapped."""
    problems = []
    for team, t in cfg.teams.items():
        for alias in t.models:
            m = cfg.models.get(alias)
            if m is None:
                problems.append(f"team '{team}' allows unknown model '{alias}'")
                continue
            for f in ("input_usd_per_mtok", "output_usd_per_mtok"):
                v = getattr(m, f)
                if v is None or not math.isfinite(v) or v <= 0:
                    problems.append(f"model '{alias}' (allowed for team '{team}') has no valid {f}")
    if problems:
        raise ConfigError("; ".join(sorted(set(problems))))


class Policy:
    def __init__(self, cfg: PolicyCfg):
        for name in list(cfg.models) + list(cfg.teams):
            if not re.match(NAME, name):
                raise ConfigError(f"invalid alias/team name '{name}'")
        check_prices(cfg)
        for team in cfg.budgets.teams:
            if team not in cfg.teams:
                raise ConfigError(f"budget for unknown team '{team}'")
        self.cfg = cfg
        self.routes = {
            a: Route(a, m.model_id, m.input_usd_per_mtok or 0, m.output_usd_per_mtok or 0, m.max_tokens,
                     frozenset(m.sampling), m.sampling_exclusive)
            for a, m in cfg.models.items()
        }
        try:
            self.secrets = {k: re.compile(v) for k, v in cfg.screening.secrets.items()}
            self.pii = {k: re.compile(v, re.I) for k, v in cfg.screening.pii.items()}
        except re.error as e:
            raise ConfigError(f"invalid screening regex: {e}") from None

    @classmethod
    def from_yaml(cls, text: str) -> "Policy":
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise ConfigError(f"policy is not valid YAML: {e}") from None
        if not isinstance(raw, dict):
            raise ConfigError("policy is empty or not a mapping")
        try:
            return cls(PolicyCfg.model_validate(raw))
        except ValidationError as e:
            first = e.errors()[0]
            raise ConfigError(f"policy invalid at {'.'.join(map(str, first['loc']))}: {first['msg']}") from None

    # ---- resolution ------------------------------------------------------------

    def _team(self, name: str) -> TeamCfg:
        team = self.cfg.teams.get(name)
        if team is None:
            raise PolicyDenied("team_not_allowed", "your team is not enabled on this gateway")
        return team

    def resolve(self, team_name: str, alias: str, max_tokens: int, sampling: dict[str, float]) -> Route:
        team = self._team(team_name)
        if alias not in team.models:  # same answer for "doesn't exist" and "not yours"
            raise PolicyDenied("model_not_allowed", f"model '{alias}' is not available to your team", param="model")
        route = self.routes[alias]
        if max_tokens > route.max_tokens:
            raise PolicyDenied("max_tokens_not_allowed", f"max_tokens for '{alias}' is capped at {route.max_tokens}",
                               param="max_tokens")
        for p in sampling:
            if p not in route.sampling:
                raise PolicyDenied("unsupported_parameter", f"unsupported_parameter: {p} (model '{alias}' does not "
                                   f"accept it)", status=400, param=p)
        if route.sampling_exclusive and len(sampling) > 1:
            raise PolicyDenied("unsupported_parameter", f"unsupported_parameter: top_p (model '{alias}' accepts "
                               "temperature or top_p, not both)", status=400, param="top_p")
        return route

    def limits(self, team_name: str, username: str) -> Limits:
        """Monthly limits with precedence user > team > default."""
        team = self._team(team_name)
        b = self.cfg.budgets
        tb = b.teams.get(team_name, TeamBudgetCfg())
        team_usd = tb.team_monthly_usd if tb.team_monthly_usd is not None else b.default.team_monthly_usd
        if username in b.users:
            user_usd = b.users[username].monthly_usd
        elif tb.user_monthly_usd is not None:
            user_usd = tb.user_monthly_usd
        else:
            user_usd = b.default.user_monthly_usd
        return Limits(usd_to_micro(team_usd), usd_to_micro(user_usd), team.user_requests_per_minute)

    def allowed_models(self, team_name: str) -> list[str]:
        return sorted(self._team(team_name).models)

    # ---- screening -------------------------------------------------------------

    def screen_input(self, texts: list[str]) -> Screened:
        out = Screened([])
        for t in texts:
            t = normalize(t)
            for name, pat in self.secrets.items():
                if pat.search(t):
                    raise PolicyDenied("sensitive_data", f"request contains a credential ({name}); remove it and retry",
                                       status=400)
            out.texts.append(self._redact_pii(t, out.redactions))
        return out

    def screen_output(self, text: str) -> tuple[str, dict[str, int]]:
        counts: dict[str, int] = {}
        text = normalize(text)
        for name, pat in self.secrets.items():
            text, n = pat.subn(f"[REDACTED:{name}]", text)
            if n:
                counts[name] = counts.get(name, 0) + n
        return self._redact_pii(text, counts), counts

    def _redact_pii(self, text: str, counts: dict[str, int]) -> str:
        for name, pat in self.pii.items():
            def sub(m: re.Match, name=name) -> str:
                if name == "credit_card" and not luhn_ok(m.group(0)):
                    return m.group(0)
                counts[name] = counts.get(name, 0) + 1
                return f"[REDACTED:{name}]"
            text = pat.sub(sub, text)
        return text


# ---- sources -------------------------------------------------------------------

def ssm_source(parameter: str, client) -> Callable[[], str]:
    def load() -> str:
        return client.get_parameter(Name=parameter, WithDecryption=True)["Parameter"]["Value"]
    return load


def file_source(path: str) -> Callable[[], str]:
    def load() -> str:
        with open(path) as f:
            return f.read()
    return load


class PolicyStore:
    """Holds the last validated policy and refreshes it on a TTL, with bounded staleness."""

    def __init__(self, source: Callable[[], str], refresh_s: float = 60, max_stale_s: float = 900,
                 clock=time.monotonic, on_reject: Callable[[str], None] | None = None):
        if max_stale_s < refresh_s:
            raise ConfigError("max_stale_s must be >= refresh_s")
        self.source = source
        self.on_reject = on_reject          # alarm hook: called on every rejected refresh
        self.refresh_s, self.max_stale_s, self.clock = refresh_s, max_stale_s, clock
        self._lock = threading.Lock()
        self._policy = self._load()          # ConfigError -> the app does not start
        self._valid_at = self._attempted_at = clock()
        self.last_error: str | None = None

    def _load(self) -> Policy:
        try:
            raw = self.source()
        except Exception as e:
            raise ConfigError(f"could not load policy: {type(e).__name__}") from None
        return Policy.from_yaml(raw)

    def current(self) -> Policy:
        now = self.clock()
        if now - self._attempted_at >= self.refresh_s:
            with self._lock:
                if self.clock() - self._attempted_at >= self.refresh_s:
                    try:
                        self._policy = self._load()
                        self._valid_at = self.clock()
                        self.last_error = None
                    except ConfigError as e:
                        self.last_error = str(e)
                        log.error("policy refresh rejected; serving last validated policy: %s", e)
                        if self.on_reject:
                            self.on_reject(str(e))
                    self._attempted_at = self.clock()
        if self.clock() - self._valid_at > self.max_stale_s:
            raise PolicyUnavailable(f"no valid policy refresh for {self.max_stale_s:.0f}s: {self.last_error}")
        return self._policy
