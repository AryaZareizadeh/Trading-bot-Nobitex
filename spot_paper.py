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
        self.model_path = Path(store.path).parent / "spot_research_model.joblib"

    def state(self):
        return self.store.get("spot_manager", "paper")

    def start(self, assets=None, budget=200):
        with self.lock:
            state = self.state()
            if state is None:
                state = new_state(budget, assets or APPROVED_ASSETS)
            elif (assets is not None and list(assets) != state["assets"]) or D(budget) != D(state["budget_usdt"]):
                raise ValueError("Existing paper portfolio has a fixed budget and universe")
            state["running"] = True
            self.store.put("spot_manager", "paper", state)
            return state

    def stop(self):
        with self.lock:
            state = self.state()
            if state:
                state["running"] = False
                self.store.put("spot_manager", "paper", state)
            return state

    def pause_on_restart(self):
        with self.lock:
            state = self.state()
            if state and state["running"]:
                state["running"] = False
                state["decisions"].append({"at": datetime.now(timezone.utc).isoformat(),
                                           "reason": "Paused after application restart; explicit start required"})
                self.store.put("spot_manager", "paper", state)

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
        for asset in selected:
            if asset not in available:
                audits[asset] = {"eligible": False, "reason": "No direct Nobitex USDT book"}
                continue
            try:
                history = self.signals.fetch(asset, "1h", start, end)
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
        result = run_experiment(signal, venue, capital=budget)
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        result["run_id"] = run_id
        snapshot = json.dumps({"signal": signal, "nobitex": venue},
                              sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        snapshot_path = self.model_path.with_name(f"spot_data_{run_id}.json.gz")
        snapshot_temp = snapshot_path.with_suffix(".tmp")
        snapshot_temp.write_bytes(gzip.compress(snapshot, compresslevel=6))
        snapshot_temp.replace(snapshot_path)
        result["data_snapshot"] = {"file": snapshot_path.name,
                                   "sha256": hashlib.sha256(snapshot).hexdigest(),
                                   "compressed_bytes": snapshot_path.stat().st_size}
        result["data_quality"] = audits
        result["eligible_assets"] = list(signal)
        result["signal_source_transfer"] = [a for a in signal if source_for(a) != "binance"]
        frames = frames_for(signal)
        method = result["selected"]
        models = None if method == "momentum" else fit_models(frames,
            min(frame.index.min() for frame in frames.values()),
            max(frame.index.max() for frame in frames.values()), method)[0]
        package = {"version": RESEARCH_VERSION, "method": method, "models": models,
                   "assets": list(signal), "data_hash": result["data_hash"],
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
        with self.lock:
            state = self.state()
            if not state or not state["running"]:
                return {"ok": False, "reason": "Paper portfolio is stopped"}
            if not self.model_path.exists():
                return {"ok": False, "reason": "Run historical research before paper decisions"}
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
            for asset in state["assets"]:
                if asset not in package["assets"] or asset not in books or asset not in rules["quantity_steps"]:
                    continue
                chosen = source_for(asset, live=True)
                try:
                    history = self.signals.fetch(asset, "1h", end - 9 * 86400, end, live=True)
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
            frames = frames_for(signal)
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
                self.store.put("spot_manager", "paper", state)
                return {"ok": False, "reason": "Missing owned-asset Nobitex valuation"}
            observed = mark(state, prices, newest)
            if end // 3600 % 4 or time.time() - end > 600:
                state["last_observation_at"] = datetime.fromtimestamp(end, timezone.utc).isoformat()
                self.store.put("spot_manager", "paper", state)
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
            self.store.put("spot_manager", "paper", state)
            return {"ok": True, "state": state}
