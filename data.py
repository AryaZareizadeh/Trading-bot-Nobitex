import time
import requests
import pandas as pd

from config import Config


class DataFetcher:
    _URL = "https://apiv2.nobitex.ir/market/udf/history"
    _PAGE = 500  # candles per request

    # Nobitex resolution string → candle duration in seconds (used to drop the open candle)
    _RESOLUTION_SECONDS: dict[str, int] = {
        "1":   60,
        "5":   300,
        "15":  900,
        "30":  1800,
        "60":  3600,
        "180": 10800,
        "240": 14400,
        "D":   86400,
        "W":   604800,
        "M":   2592000,
    }

    def __init__(self, config: Config):
        self.config = config

    def fetch(self) -> pd.DataFrame:
        resolution = self.config.interval
        candle_secs = self._RESOLUTION_SECONDS[resolution]
        total = self.config.limit
        to_time = int(time.time())
        rows: list[tuple] = []

        while len(rows) < total:
            params = {
                "symbol": self.config.symbol.lower(),
                "resolution": resolution,
                "countback": self._PAGE,
                "to": to_time,
            }
            response = requests.get(self._URL, params=params)
            response.raise_for_status()
            data = response.json()

            if data.get("s") != "ok" or not data.get("t"):
                break

            timestamps = data["t"]
            batch = list(zip(timestamps, data["o"], data["h"], data["l"], data["c"], data["v"]))
            rows = batch + rows  # prepend to keep chronological order
            to_time = timestamps[0] - 1  # move before the oldest candle

            if len(timestamps) < self._PAGE:
                break  # no more history available

        raw = pd.DataFrame(rows, columns=["open_time", "Open", "High", "Low", "Close", "Volume"])
        raw = raw.drop_duplicates(subset="open_time").sort_values("open_time")

        # Drop the still-open candle at the leading edge
        now = int(time.time())
        raw = raw[raw["open_time"] + candle_secs <= now].tail(total).reset_index(drop=True)

        df = raw[["Open", "High", "Low", "Close", "Volume"]].astype(float)
        df.index = pd.to_datetime(raw["open_time"], unit="s")
        df.index.name = "Date"
        return df
