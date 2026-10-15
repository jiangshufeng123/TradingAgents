"""The data gateway: one entry point per kind of data, several sources behind it.

Each *capability* (daily bars, the whole-market quote table, 龙虎榜, margin...)
has an ordered list of *providers*. ``Gateway.fetch`` tries them in order and
returns the first answer that passes the capability's quality check:

* a provider that is not configured (no token, library missing) is skipped;
* a provider that raises, or answers with data that fails the quality check,
  is recorded as a failure and the next one is tried;
* after ``breaker_failures`` failures in a row a provider's circuit opens and it
  is skipped for ``breaker_seconds``; then one request is let through again,
  and a success closes the circuit;
* an empty answer means "no data here", not a fault: the next provider is
  asked, unless the capability accepts empty answers (a stock that was not on
  the 龙虎榜 is a valid answer).

Answers can be cached on disk, shared by every process using the same cache
folder (the CLI, the scheduled run and the web service). Each provider's record
(successes, failures, last error, latency, circuit state) is kept in
``<data_cache_dir>/gateway/health.json`` for the status page.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pickle
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError

logger = logging.getLogger(__name__)


class NotConfigured(Exception):
    """The provider cannot be used here (no token, library missing); skipped quietly."""


class QualityError(ValueError):
    """The provider answered, but the data looks wrong."""


class GatewayUnavailableError(VendorUnavailableError):
    """Every provider for a capability failed or was skipped."""

    def __init__(self, capability: str, attempts: list[tuple[str, str]]):
        self.capability = capability
        self.attempts = attempts
        detail = "; ".join(f"{p}: {why}" for p, why in attempts) or "no provider registered"
        super().__init__(f"{capability}: {detail}")


class GatewayNoDataError(NoMarketDataError):
    """Every provider answered, none had data for the request."""

    def __init__(self, capability: str, attempts: list[tuple[str, str]]):
        self.capability = capability
        self.attempts = attempts
        Exception.__init__(self, f"{capability}: no data ({'; '.join(f'{p}: {w}' for p, w in attempts)})")


@dataclass
class Provider:
    name: str                    # short id used in config, e.g. "tushare"
    label: str                   # shown to people, e.g. "Tushare Pro"
    fn: Callable[..., Any]
    configured: Callable[[], str | None] = lambda: None   # a reason when it cannot be used


@dataclass
class Capability:
    name: str
    title: str
    providers: list[Provider] = field(default_factory=list)
    check: Callable[[Any, dict], None] | None = None     # raises QualityError
    empty_ok: bool = False


@dataclass
class Result:
    data: Any
    source: str                   # the provider's label
    provider: str                 # the provider's name
    attempts: list[tuple[str, str]]
    cached: bool = False


def is_empty(data) -> bool:
    if data is None:
        return True
    if isinstance(data, pd.DataFrame):
        return data.empty
    if isinstance(data, (list, dict, tuple)):
        return len(data) == 0
    return False


# --- provider health and circuit breaking ------------------------------------------

class Health:
    """Per (capability, provider) counters and circuit state, mirrored to a JSON file."""

    def __init__(self, path: Path | None, failures: int = 3, open_seconds: float = 600):
        self.path = path
        self.failures = failures
        self.open_seconds = open_seconds
        self._lock = threading.Lock()
        self._stats: dict[str, dict] = {}
        self._load()

    @staticmethod
    def key(capability: str, provider: str) -> str:
        return f"{capability}/{provider}"

    def _load(self) -> None:
        if self.path and self.path.exists():
            try:
                self._stats = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._stats = {}

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(self._stats, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:        # the record is a convenience; never fail a fetch over it
            logger.debug("gateway health not saved: %s", exc)

    def _entry(self, capability: str, provider: str) -> dict:
        return self._stats.setdefault(self.key(capability, provider), {
            "ok": 0, "fail": 0, "streak": 0, "last_ok": None, "last_fail": None,
            "last_error": None, "latency_ms": None, "open_until": None,
        })

    def allow(self, capability: str, provider: str, now: float | None = None) -> bool:
        with self._lock:
            entry = self._stats.get(self.key(capability, provider))
            if not entry or not entry.get("open_until"):
                return True
            return (now or time.time()) >= entry["open_until"]

    def ok(self, capability: str, provider: str, latency: float) -> None:
        with self._lock:
            e = self._entry(capability, provider)
            e["ok"] += 1
            e["streak"] = 0
            e["open_until"] = None
            e["last_ok"] = time.time()
            e["latency_ms"] = round(latency * 1000)
            self._save()

    def fail(self, capability: str, provider: str, error: str) -> None:
        with self._lock:
            e = self._entry(capability, provider)
            e["fail"] += 1
            e["streak"] += 1
            e["last_fail"] = time.time()
            e["last_error"] = " ".join(str(error).split())[:300]
            if e["streak"] >= self.failures:
                e["open_until"] = time.time() + self.open_seconds
            self._save()

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            self._load_merge()
            return json.loads(json.dumps(self._stats))

    def _load_merge(self) -> None:
        """Take in what other processes recorded since (newest event wins per entry)."""
        if not (self.path and self.path.exists()):
            return
        try:
            theirs = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for k, v in theirs.items():
            mine = self._stats.get(k)
            stamp = lambda d: max(d.get("last_ok") or 0, d.get("last_fail") or 0)   # noqa: E731
            if mine is None or stamp(v) > stamp(mine):
                self._stats[k] = v

    def reset(self) -> None:
        with self._lock:
            self._stats = {}
            self._save()


# --- the gateway ----------------------------------------------------------------

def parse_order(text: str | None) -> dict[str, list[str]]:
    """``"daily_bars=sina,tushare; cn_spot=tushare"`` -> {capability: [providers]}."""
    order = {}
    for part in (text or "").split(";"):
        if "=" in part:
            cap, names = part.split("=", 1)
            order[cap.strip()] = [n.strip() for n in names.split(",") if n.strip()]
    return order


class Gateway:
    def __init__(self, cache_dir: str | Path | None = None, order: str | None = None,
                 breaker_failures: int = 3, breaker_seconds: float = 600):
        self.cache_dir = Path(cache_dir).expanduser() / "gateway" if cache_dir else None
        self.order = parse_order(order)
        self.capabilities: dict[str, Capability] = {}
        self.health = Health(self.cache_dir / "health.json" if self.cache_dir else None,
                             breaker_failures, breaker_seconds)

    # registration
    def capability(self, name: str, title: str, check=None, empty_ok: bool = False) -> Capability:
        cap = self.capabilities.setdefault(name, Capability(name, title))
        cap.title, cap.check, cap.empty_ok = title, check, empty_ok
        return cap

    def provider(self, capability: str, name: str, label: str, fn, configured=None) -> None:
        cap = self.capabilities[capability]
        new = Provider(name, label, fn, configured or (lambda: None))
        for i, p in enumerate(cap.providers):
            if p.name == name:          # replacing a source keeps its place in the order
                cap.providers[i] = new
                return
        cap.providers.append(new)

    def providers(self, capability: str) -> list[Provider]:
        cap = self.capabilities[capability]
        wanted = self.order.get(capability)
        if not wanted:
            return list(cap.providers)
        by_name = {p.name: p for p in cap.providers}
        return [by_name[n] for n in wanted if n in by_name]

    # caching
    def _cache_path(self, capability: str, kwargs: dict, cache_dir: Path | None) -> Path | None:
        base = cache_dir or self.cache_dir
        if base is None:
            return None
        digest = hashlib.sha1(json.dumps(kwargs, sort_keys=True, default=str).encode()).hexdigest()[:20]
        return base / "cache" / capability / f"{digest}.pkl"

    @staticmethod
    def _read_cache(path: Path | None, ttl: float):
        if path is None or not path.exists() or time.time() - path.stat().st_mtime >= ttl:
            return None
        try:
            with path.open("rb") as fh:
                return pickle.load(fh)
        except Exception:
            return None

    @staticmethod
    def _write_cache(path: Path | None, payload) -> None:
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            with tmp.open("wb") as fh:
                pickle.dump(payload, fh)
            os.replace(tmp, path)
        except Exception as exc:
            logger.debug("gateway cache not written: %s", exc)

    # fetching
    def fetch(self, capability: str, *, ttl: float | None = None, cache_dir: str | Path | None = None,
              **kwargs) -> Result:
        """The first good answer for ``capability(**kwargs)``.

        ``ttl`` seconds: reuse a cached answer that young (shared on disk).
        Raises GatewayNoDataError when every provider answered with nothing,
        GatewayUnavailableError when none could answer.
        """
        cap = self.capabilities[capability]
        cache_base = Path(cache_dir).expanduser() / "gateway" if cache_dir else None
        path = self._cache_path(capability, kwargs, cache_base) if ttl else None
        if ttl:
            hit = self._read_cache(path, ttl)
            if hit is not None:
                return Result(hit["data"], hit["source"], hit["provider"], [], cached=True)

        attempts: list[tuple[str, str]] = []
        answered_empty = False
        for p in self.providers(capability):
            try:
                reason = p.configured()
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
            if reason:
                attempts.append((p.label, f"skipped ({reason})"))
                continue
            if not self.health.allow(capability, p.name):
                attempts.append((p.label, "skipped (circuit open after repeated failures)"))
                continue
            started = time.time()
            try:
                data = p.fn(**kwargs)
            except NotConfigured as exc:
                attempts.append((p.label, f"skipped ({exc})"))
                continue
            except NoMarketDataError as exc:      # nothing for this request (or not its market):
                attempts.append((p.label, f"no data ({exc})"))   # says nothing about its health
                answered_empty = True
                continue
            except Exception as exc:
                why = f"{type(exc).__name__}: {exc}"
                self.health.fail(capability, p.name, why)
                attempts.append((p.label, why))
                logger.info("gateway %s via %s failed: %s", capability, p.label, why)
                continue
            if is_empty(data):
                self.health.ok(capability, p.name, time.time() - started)
                if cap.empty_ok:
                    return self._done(capability, data, p, attempts, path)
                attempts.append((p.label, "no data"))
                answered_empty = True
                continue
            if cap.check:
                try:
                    cap.check(data, kwargs)
                except QualityError as exc:
                    self.health.fail(capability, p.name, f"quality: {exc}")
                    attempts.append((p.label, f"bad data ({exc})"))
                    logger.warning("gateway %s: %s answered bad data: %s", capability, p.label, exc)
                    continue
            self.health.ok(capability, p.name, time.time() - started)
            return self._done(capability, data, p, attempts, path)
        failed = [a for a in attempts if not a[1].startswith(("no data", "skipped"))]
        if answered_empty and not failed:
            raise GatewayNoDataError(capability, attempts)
        raise GatewayUnavailableError(capability, attempts)

    def _done(self, capability, data, p: Provider, attempts, path) -> Result:
        if path is not None:
            self._write_cache(path, {"data": data, "source": p.label, "provider": p.name})
        return Result(data, p.label, p.name, attempts + [(p.label, "ok")])

    def probe(self, capability: str, provider: str, **kwargs) -> tuple[str, str, float]:
        """Ask one provider directly (ignoring its circuit), record the outcome.

        Returns (state, detail, seconds); state is ok / empty / bad data / failed / not configured.
        """
        cap = self.capabilities[capability]
        p = next(x for x in cap.providers if x.name == provider)
        reason = p.configured()
        if reason:
            return "not configured", reason, 0.0
        started = time.time()
        try:
            data = p.fn(**kwargs)
        except NotConfigured as exc:
            return "not configured", str(exc), time.time() - started
        except NoMarketDataError as exc:
            return "empty", str(exc), time.time() - started
        except Exception as exc:
            why = f"{type(exc).__name__}: {exc}"
            self.health.fail(capability, p.name, why)
            return "failed", " ".join(why.split())[:200], time.time() - started
        took = time.time() - started
        if is_empty(data):
            self.health.ok(capability, p.name, took)
            return "empty", "answered with no rows", took
        if cap.check:
            try:
                cap.check(data, kwargs)
            except QualityError as exc:
                self.health.fail(capability, p.name, f"quality: {exc}")
                return "bad data", str(exc), took
        self.health.ok(capability, p.name, took)
        size = f"{len(data)} rows" if hasattr(data, "__len__") else "1 record"
        return "ok", size, took

    # status
    def status(self) -> list[dict]:
        """One row per (capability, provider) in priority order, for the status page."""
        snap = self.health.snapshot()
        now = time.time()
        rows = []
        for cap in self.capabilities.values():
            for rank, p in enumerate(self.providers(cap.name), 1):
                try:
                    reason = p.configured()
                except Exception as exc:
                    reason = str(exc)
                s = snap.get(Health.key(cap.name, p.name), {})
                open_until = s.get("open_until")
                if reason:
                    state = "not configured"
                elif open_until and open_until > now:
                    state = "circuit open"
                elif not s:
                    state = "unused"
                elif s.get("streak"):
                    state = "failing"
                else:
                    state = "ok"
                rows.append({
                    "capability": cap.name, "title": cap.title, "rank": rank, "provider": p.name,
                    "label": p.label, "state": state, "note": reason or "",
                    "ok": s.get("ok", 0), "fail": s.get("fail", 0), "streak": s.get("streak", 0),
                    "last_ok": s.get("last_ok"), "last_fail": s.get("last_fail"),
                    "last_error": s.get("last_error"), "latency_ms": s.get("latency_ms"),
                    "open_until": open_until if open_until and open_until > now else None,
                })
        return rows
