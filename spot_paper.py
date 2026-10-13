"""Credential-free paper portfolio service. This module has no live-order path."""

from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path
import gzip
import hashlib
import json
import shutil
import statistics
import threading
import time
import uuid

import joblib
import requests

from spot_data import APPROVED_ASSETS, NobitexPublicSpot, PublicSpotData, source_for
from spot_portfolio import D, SpotCosts, SpotRisk, book_quote, execute_targets, mark, new_state, target_weights
from spot_research import RESEARCH_VERSION, _best_forecasts, fit_models, forecasts_for, frames_for, run_experiment


PAPER_HISTORY_LIMIT = 500
PAPER_HISTORY_KINDS = ("equity", "fills", "orders", "decisions")


def _overlap(a, b):
    ordered = sorted(b, key=lambda bar: bar["available_at"])
    times = [datetime.fromisoformat(bar["available_at"]) for bar in ordered]
    differences, lags = [], []
    for bar in a:
        when = datetime.fromisoformat(bar["available_at"])
        index = bisect_right(times, when) - 1
        if index < 0:
            continue
        lag = (when - times[index]).total_seconds()
        if lag > 3600:
            continue
        differences.append(100 * (bar["close"] / ordered[index]["close"] - 1))
        lags.append(lag / 60)
    if len(differences) < 24:
        return {"overlap_hours": len(differences), "assessment": "insufficient overlap"}
    return {"overlap_hours": len(differences), "median_lag_minutes": statistics.median(lags),
            "median_first_vs_second_pct": round(statistics.median(differences), 4),
            "max_absolute_difference_pct": round(max(abs(v) for v in differences), 4)}


class SpotPaperService:
    def __init__(self, store, *, signals=None, venue=None):
        self.store = store
        self.signals = signals or PublicSpotData()
        self.venue = venue or NobitexPublicSpot()
        self.lock = threading.RLock()
        self.tick_lock = threading.Lock()
        self.generation = 0
        self.model_lock = threading.RLock()
        self.model_path = Path(store.path).parent / "spot_research_model.joblib"

    def state(self):
        with self.lock:
            state = self.store.get("spot_manager", "paper")
            if state is not None and "history_id" not in state:
                # Migrate the complete legacy lists before bounding UI tails.
                self._save_state(state)
            return state

    def history(self, kind, *, session_id=None, limit=PAPER_HISTORY_LIMIT):
        """Read append-only session history; limit=None is an explicit full export."""
        if kind not in PAPER_HISTORY_KINDS:
            raise ValueError("Unknown paper history kind")
        if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0):
            raise ValueError("History limit must be a positive integer or None")
        if session_id is None:
            state = self.state()
            if not state:
                return []
            session_id = state["history_id"]
        prefix = f"{session_id}:{kind}:"
        with self.store.connection() as conn:
            query = "SELECT value FROM records WHERE namespace='spot_paper_history' AND key>=? AND key<? ORDER BY key DESC"
            args = [prefix, prefix + "\uffff"]
            if limit is not None:
                query += " LIMIT ?"
                args.append(limit)
            rows = conn.execute(query, args).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]

    def _save_state(self, state):
        """Commit new immutable events and a bounded state snapshot atomically."""
        history_id = state.get("history_id") or uuid.uuid4().hex
        counts = dict(state.get("history_counts", {}))
        bounded = dict(state)
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for kind in PAPER_HISTORY_KINDS:
                total = counts.get(kind, 0)
                entries = state.get(kind, [])
                # A loaded state contains min(total, limit) already archived
                # events followed by the current tick's newly appended events.
                pending = entries[min(total, PAPER_HISTORY_LIMIT):]
                for index, entry in enumerate(pending, total + 1):
                    conn.execute("INSERT INTO records(namespace,key,value) VALUES(?,?,?)",
                                 ("spot_paper_history", f"{history_id}:{kind}:{index:020d}",
                                  json.dumps(entry, allow_nan=False)))
                counts[kind] = total + len(pending)
                bounded[kind] = entries[-PAPER_HISTORY_LIMIT:]
            bounded.update(history_id=history_id, history_counts=counts, history_limit=PAPER_HISTORY_LIMIT,
                           history_version=1)
            self.store.write(conn, "spot_manager", "paper", bounded)
        state.clear()
        state.update(bounded)

    def model_status(self):
        """Read readiness from the current artifact, including portfolio coverage."""
        status = {"exists": self.model_path.exists(), "ready": False, "reason": None,
                  "version": None, "method": None, "assets": [],
                  "missing_paper_assets": [], "trained_at": None, "run_id": None}
        if not status["exists"]:
            status["reason"] = "Run historical research before paper decisions"
            return status
        try:
            # Read the actual file each time: rapid same-size rewrites can have
            # identical Windows timestamps. Do not block on market-data ticks.
            with self.model_lock:
                package = joblib.load(self.model_path)
            if not isinstance(package, dict):
                raise ValueError("Invalid paper model package")
            assets = package.get("assets")
            if (not isinstance(assets, list) or not assets
                    or any(not isinstance(a, str) or a not in APPROVED_ASSETS for a in assets)):
                raise ValueError("Invalid paper model assets")
            metadata = {key: package.get(key) for key in ("version", "method", "trained_at", "run_id")}
            metadata["assets"] = assets
            metadata["complete"] = (package.get("method") in {"momentum", "hgb", "lightgbm"}
                and "models" in package
                and (package["method"] == "momentum" or package["models"] is not None))
            for key in ("version", "method", "trained_at", "run_id"):
                status[key] = metadata.get(key) if isinstance(metadata.get(key), str) else None
            status["assets"] = list(metadata["assets"])
            paper = self.state()
            if paper:
                status["missing_paper_assets"] = [a for a in paper["assets"] if a not in status["assets"]]
            if status["version"] != RESEARCH_VERSION:
                status["reason"] = "Model version mismatch; run historical research again"
            elif not metadata["complete"]:
                status["reason"] = "Saved paper model is incomplete; run historical research again"
            elif paper and not set(paper["assets"]).intersection(status["assets"]):
                status["reason"] = "Research model does not cover this paper portfolio's assets"
            else:
                status["ready"] = True
        except Exception:
            status["reason"] = "Saved paper model could not be read; run historical research again"
        return status

    def start(self, assets=None, budget=200):
        with self.lock:
            state = self.state()
            if state is None:
                state = new_state(budget, assets or APPROVED_ASSETS)
            elif (assets is not None and list(assets) != state["assets"]) or D(budget) != D(state["budget_usdt"]):
                raise ValueError("Existing paper portfolio has a fixed budget and universe")
            state["running"] = True
            self._save_state(state)
            self.generation += 1
            self.tick_lock = threading.Lock()
            return state

    def stop(self):
        with self.lock:
            state = self.state()
            if state:
                state["running"] = False
                self._save_state(state)
                self.generation += 1
                self.tick_lock = threading.Lock()
            return state

    def reset(self):
        """Archive the simulated session and clear it in one local transaction."""
        with self.lock:
            state = self.state()
            backup_id = None
            reset_at = datetime.now(timezone.utc).isoformat()
            with self.store.connection() as conn:
                if state is not None:
                    backup_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
                    self.store.write(conn, "spot_paper_backups", backup_id,
                                     {"reset_at": reset_at, "paper": state,
                                      "history_id": state.get("history_id"),
                                      "history_counts": state.get("history_counts", {}),
                                      "paper_error": self.store.get("spot_manager", "paper_error")})
                self.store.write(conn, "spot_manager", "paper", None)
                self.store.write(conn, "spot_manager", "paper_error", None)
            self.generation += 1
            # A stalled old market request must not block a fresh session.
            self.tick_lock = threading.Lock()
            return {"ok": True, "paper": None, "paper_error": None, "backup_id": backup_id}

    def pause_on_restart(self):
        with self.lock:
            state = self.state()
            if state and state["running"]:
                state["running"] = False
                state["decisions"].append({"at": datetime.now(timezone.utc).isoformat(),
                                           "reason": "Paused after application restart; explicit start required"})
                self._save_state(state)
                self.generation += 1
                self.tick_lock = threading.Lock()

    def research(self, assets, days=365, budget=200):
        """Public acquisition and reproducible validation, no private API calls."""
        if not 90 <= days <= 730:
            raise ValueError("Research range must be 90–730 days")
        selected = tuple(dict.fromkeys(str(a).upper() for a in assets))
        if not selected or any(a not in APPROVED_ASSETS for a in selected):
            raise ValueError("Invalid approved universe")
        end = int(time.time() // 3600 * 3600)
        start = end - days * 86400
        available = self.venue.books()
        signal, venue, audits = {}, {}, {}
        try:
            btc_history = self.signals.fetch("BTC", "1h", start, end)
        except (ValueError, OSError, requests.RequestException) as exc:
            raise ValueError("BTC context unavailable; research abstained") from exc
        btc_context = btc_history["candles"]
        if btc_history["valid"] < 96:
            raise ValueError("BTC context unavailable; research abstained")
        for asset in selected:
            if asset not in available:
                audits[asset] = {"eligible": False, "reason": "No direct Nobitex USDT book"}
                continue
            try:
                history = btc_history if asset == "BTC" else self.signals.fetch(asset, "1h", start, end)
                actual = self.venue.history(asset, "1h", start, end)
                audits[asset] = {"eligible": history["valid"] >= 200 and actual["valid"] >= 200,
                                 "signal_source": source_for(asset),
                                 "signal": {k: v for k, v in history.items() if k != "candles"},
                                 "nobitex": {k: v for k, v in actual.items() if k != "candles"}}
                if audits[asset]["eligible"]:
                    signal[asset], venue[asset] = history["candles"], actual["candles"]
                    audits[asset]["signal_vs_nobitex"] = _overlap(history["candles"], actual["candles"])
                    if source_for(asset) != "binance":
                        try:
                            recent = self.signals.fetch(asset, "1h", max(start, end - 30 * 86400), end, live=True)
                            audits[asset]["supplement_vs_binance"] = _overlap(history["candles"], recent["candles"])
                        except (ValueError, OSError, requests.RequestException) as exc:
                            audits[asset]["supplement_vs_binance"] = {"assessment": str(exc)[:120]}
            except (ValueError, OSError, requests.RequestException) as exc:
                audits[asset] = {"eligible": False, "reason": str(exc)[:160]}
        if not signal:
            raise ValueError("No asset has both valid spot signal and Nobitex history")
        result = run_experiment(signal, venue, capital=budget, btc_context=btc_context)
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        result["run_id"] = run_id
        snapshot = json.dumps({"signal": signal, "nobitex": venue, "btc_context": btc_context},
                              sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        snapshot_path = self.model_path.with_name(f"spot_data_{run_id}.json.gz")
        snapshot_temp = snapshot_path.with_suffix(".tmp")
        snapshot_temp.write_bytes(gzip.compress(snapshot, compresslevel=6))
        snapshot_temp.replace(snapshot_path)
        result["data_snapshot"] = {"file": snapshot_path.name,
                                   "sha256": hashlib.sha256(snapshot).hexdigest(),
                                   "compressed_bytes": snapshot_path.stat().st_size}
        result["data_quality"] = audits
        result["btc_context"] = {"source": "binance", "valid": btc_history["valid"],
                                 "allocation_asset": "BTC" in signal}
        result["eligible_assets"] = list(signal)
        result["signal_source_transfer"] = [a for a in signal if source_for(a) != "binance"]
        frames = frames_for(signal, btc_context)
        method = result["selected"]
        models = None if method == "momentum" else fit_models(frames,
            min(frame.index.min() for frame in frames.values()),
            max(frame.index.max() for frame in frames.values()), method)[0]
        package = {"version": RESEARCH_VERSION, "method": method, "models": models,
                   "assets": list(signal), "data_hash": result["data_hash"],
                   "btc_context_source": "binance", "feature_policy": "Required actual BTC context; missing context abstains",
                   "run_id": run_id,
                   "trained_at": datetime.now(timezone.utc).isoformat()}
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        artifact = self.model_path.with_name(f"spot_model_{run_id}.joblib")
        temporary = artifact.with_suffix(".tmp")
        joblib.dump(package, temporary)
        temporary.replace(artifact)
        result["paper_model"] = {k: v for k, v in package.items() if k != "models"}
        result["model_artifact"] = artifact.name
        result["model_file_sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
        with self.lock:
            with self.model_lock:
                latest_temp = self.model_path.with_suffix(".tmp")
                shutil.copyfile(artifact, latest_temp)
                latest_temp.replace(self.model_path)
            self.store.put("spot_experiments", run_id, result)
            self.store.put("spot_manager", "research", result)
            runs = self.store.get("spot_manager", "experiments", [])
            runs.append({"run_id": run_id, "selected": method, "passed": result["validation_outcome"]["passed"],
                         "data_hash": result["data_hash"], "code_hash": result["code_hash"],
                         "windows": result["windows"]})
            self.store.put("spot_manager", "experiments", runs)
        return result

    def tick(self):
        # Network acquisition can stall. Hold the state lock only while reading
        # or committing, so pause/reset stay available throughout an update.
        with self.lock:
            state = self.state()
            generation = self.generation
            tick_lock = self.tick_lock
            if not state or not state["running"]:
                return {"ok": False, "reason": "Paper portfolio is stopped"}
            if not tick_lock.acquire(blocking=False):
                return {"ok": False, "reason": "Paper update already running"}
        try:
            return self._tick(state, generation)
        finally:
            tick_lock.release()

    def _save_tick(self, state, generation):
        with self.lock:
            if generation != self.generation:
                return False
            self._save_state(state)
            return True

    def _tick(self, state, generation):
        if not self.model_path.exists():
            return {"ok": False, "reason": "Run historical research before paper decisions"}
        with self.model_lock:
            package = joblib.load(self.model_path)
        if package["version"] != RESEARCH_VERSION:
            return {"ok": False, "reason": "Model version mismatch"}
        end = int(time.time() // 3600 * 3600)
        if state.get("last_observation_at") == datetime.fromtimestamp(end, timezone.utc).isoformat():
            return {"ok": True, "reason": "Already processed this closed hour"}
        books = self.venue.books()
        rules = self.venue.rules()
        signal = {}
        sources = {}
        try:
            btc_history = self.signals.fetch("BTC", "1h", end - 9 * 86400, end, live=True)
        except (ValueError, OSError, requests.RequestException):
            return {"ok": False, "reason": "BTC context unavailable; paper decisions abstained"}
        if btc_history["valid"] < 96:
            return {"ok": False, "reason": "BTC context unavailable; paper decisions abstained"}
        for asset in state["assets"]:
            if asset not in package["assets"] or asset not in books or asset not in rules["quantity_steps"]:
                continue
            chosen = source_for(asset, live=True)
            try:
                history = btc_history if asset == "BTC" else self.signals.fetch(asset, "1h", end - 9 * 86400, end, live=True)
                if history["valid"] < 96 and source_for(asset) != "binance":
                    history = self.signals.fetch(asset, "1h", end - 9 * 86400, end)
                    chosen = source_for(asset)
                if history["valid"] >= 96:
                    signal[asset] = history["candles"]
                    sources[asset] = chosen
            except (ValueError, OSError, requests.RequestException):
                continue
        if not signal:
            return {"ok": False, "reason": "No complete live spot signal history"}
        frames = frames_for(signal, btc_history["candles"])
        start = datetime.fromtimestamp(end - 3600, timezone.utc).isoformat()
        finish = datetime.fromtimestamp(end + 1, timezone.utc).isoformat()
        forecast = forecasts_for(frames, start, finish, package["method"], package["models"])
        newest = max(forecast, default=None)
        if not newest:
            return {"ok": False, "reason": "No complete feature row"}
        if datetime.fromisoformat(newest).timestamp() != end:
            return {"ok": False, "reason": "Latest signal candle is stale"}
        predictions = _best_forecasts(forecast[newest])
        quotes = {}
        for asset, book in books.items():
            try:
                quotes[asset] = book_quote(book)
            except (ValueError, KeyError, TypeError):
                pass
        prices = {a: float((q["bid"] + q["ask"]) / 2) for a, q in quotes.items()}
        owned = {a for a, qty in state["holdings"].items() if D(qty) > 0}
        if not owned.issubset(prices):
            state["decisions"].append({"at": newest, "reason": "Missing or stale Nobitex valuation; entries paused"})
            if not self._save_tick(state, generation):
                return {"ok": False, "discarded": True, "reason": "Paper update discarded after portfolio changed"}
            return {"ok": False, "reason": "Missing owned-asset Nobitex valuation"}
        observed = mark(state, prices, newest)
        if end // 3600 % 4 or time.time() - end > 600:
            state["last_observation_at"] = datetime.fromtimestamp(end, timezone.utc).isoformat()
            if not self._save_tick(state, generation):
                return {"ok": False, "discarded": True, "reason": "Paper update discarded after portfolio changed"}
            return {"ok": True, "reason": "Hourly valuation recorded; decision window closed or not due"}
        costs = SpotCosts(min_notional=D(rules["min_notional"]) + D("0.01"))
        targets, reason = target_weights(predictions, state, observed["equity_usdt"], costs=costs)
        state["decisions"].append({"at": newest, "reason": reason, "signal_sources": sources,
                                   "forecast": predictions})
        execute_targets(state, targets, quotes, prices, costs=costs,
                        quantity_steps=rules["quantity_steps"], at=newest,
                        reason=reason, model_version=package["version"] + ":" + package["method"],
                        liquidation_prices={a: float(q["bid"] * (1 - costs.fee_rate)) for a, q in quotes.items()})
        state["last_observation_at"] = datetime.fromtimestamp(end, timezone.utc).isoformat()
        if not self._save_tick(state, generation):
            return {"ok": False, "discarded": True, "reason": "Paper update discarded after portfolio changed"}
        return {"ok": True, "state": state}
