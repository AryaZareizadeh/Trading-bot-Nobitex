import pandas as pd
import mplfinance as mpf

from config import Config


class Chart:
    _LABEL_COLORS = {
        "K": "#6AE451",
        "D": "#ff4444",
        "RSI": "#9467bd",
        "MA": "#ebff0e",
        "K-D": "#5599ff",
    }

    def __init__(self, df: pd.DataFrame, config: Config):
        self.df = df
        self.config = config

    def plot(
        self,
        symbol: str = "ZEC/USDT",
        title: str = "",
        overlays: list = None,
        subplots: list = None,
    ):
        """
        overlays  – list of mpf.make_addplot objects drawn on the price panel (panel=0).
        subplots  – list of subplot specs. Each spec is either:
                      • a callable  (panel) -> list[addplot]
                      • a tuple     ((panel) -> list[addplot],  [(label, series), ...])
        Returns (fig, axes).
        """
        all_addplots = list(overlays or [])
        num_subplots = len(subplots) if subplots else 0
        subplot_series_list = []

        for panel_num, sub in enumerate(subplots or [], start=1):
            if isinstance(sub, tuple):
                make_fn, series = sub
                subplot_series_list.append(series)
            else:
                make_fn = sub
                subplot_series_list.append([])
            all_addplots.extend(make_fn(panel_num))

        panel_ratios = (3,) + (1,) * num_subplots
        full_title = f"{symbol} - {title}" if title else symbol

        fig, axes = mpf.plot(
            self.df,
            type="candle",
            style="nightclouds",
            addplot=all_addplots if all_addplots else None,
            title=full_title,
            ylabel="Price (USDT)",
            panel_ratios=panel_ratios,
            figscale=1.5,
            figratio=(16, 6 + 2 * num_subplots),
            returnfig=True,
        )

        for ax in fig.axes:
            ax.grid(True, alpha=0.15)

        self._add_crosshair(fig, axes, subplot_series_list)
        return fig, axes

    def _add_crosshair(self, fig, axes, subplot_series=None):
        subplot_series = subplot_series or []
        # mplfinance creates 2 axes per panel; primary axes are at even indices.
        panel_axes = list(axes)[::2]
        price_ax = panel_axes[0]
        subplot_axes = panel_axes[1: 1 + len(subplot_series)]

        vlines = [
            ax.axvline(x=0, color="white", linewidth=0.8, linestyle=":", visible=False,
                       zorder=5, animated=True)
            for ax in fig.axes
        ]

        ohlc_text = price_ax.text(
            0.01, 0.98, "",
            transform=price_ax.transAxes,
            fontsize=9, color="white", va="top", ha="left", zorder=6, animated=True,
            bbox=dict(boxstyle="round,pad=0.3", facecolor="#1a1a2e", alpha=0.85),
        )

        subplot_text_groups = []
        for ax, series_list in zip(subplot_axes, subplot_series):
            group = []
            for label, s in series_list:
                color = self._LABEL_COLORS.get(label, "white")
                t = ax.text(
                    0.01, 0.98, "",
                    transform=ax.transAxes,
                    fontsize=9, color=color, va="top", ha="left", zorder=6, animated=True,
                    bbox=dict(boxstyle="round,pad=0.2", facecolor="#1a1a2e", alpha=0.75),
                )
                group.append((label, s, t))
            subplot_text_groups.append(group)

        _bg = [None]
        df = self.df

        def _save_bg(event=None):
            renderer = fig.canvas.get_renderer()
            for group in subplot_text_groups:
                if not group:
                    continue
                ax = group[0][2].axes
                ax_width_px = ax.get_window_extent(renderer).width
                x = 0.01
                for label, s, t in group:
                    t.set_position((x, 0.98))
                    t.set_text(f"{label}: -99.99")
                    text_w_px = t.get_window_extent(renderer).width
                    t.set_text("")
                    x += text_w_px / ax_width_px + 0.015
            _bg[0] = fig.canvas.copy_from_bbox(fig.bbox)

        def on_move(event):
            if _bg[0] is None:
                return
            fig.canvas.restore_region(_bg[0])
            if event.xdata is not None:
                idx = max(0, min(int(round(event.xdata)), len(df) - 1))
                for vl in vlines:
                    vl.set_xdata([idx])
                    vl.set_visible(True)
                    vl.axes.draw_artist(vl)
                row = df.iloc[idx]
                date_str = df.index[idx].strftime("%Y-%m-%d %H:%M")
                candle_color = "#00aa00" if row["Close"] >= row["Open"] else "#d62728"
                ohlc_text.set_color(candle_color)
                ohlc_text.set_text(
                    f"{date_str}   O: {row['Open']:.2f}   H: {row['High']:.2f}"
                    f"   L: {row['Low']:.2f}   C: {row['Close']:.2f}"
                )
                price_ax.draw_artist(ohlc_text)
                for group in subplot_text_groups:
                    for label, s, t in group:
                        val = s.iloc[idx] if idx < len(s) else float("nan")
                        t.set_text(f"{label}: {val:.2f}" if not pd.isna(val) else f"{label}: -")
                        t.axes.draw_artist(t)
            fig.canvas.blit(fig.bbox)

        fig.canvas.mpl_connect("draw_event", _save_bg)
        fig.canvas.mpl_connect("motion_notify_event", on_move)

    # ------------------------------------------------------------------
    # Subplot builders
    # ------------------------------------------------------------------

    def srsi_subplot(self, k: pd.Series, d: pd.Series):
        def make(panel):
            upper = pd.Series(80, index=k.index)
            lower = pd.Series(20, index=k.index)
            return [
                mpf.make_addplot(k,     panel=panel, color="#6AE451", width=1,   ylabel="Stoch RSI"),
                mpf.make_addplot(d,     panel=panel, color="#ff4444", width=1),
                mpf.make_addplot(upper, panel=panel, color="#838383", width=0.8, linestyle="dashdot"),
                mpf.make_addplot(lower, panel=panel, color="#838383", width=0.8, linestyle="dashdot"),
            ]
        return make, [("K", k), ("D", d)]

    def rsi_subplot(self, rsi: pd.Series, rsi_ma: pd.Series):
        def make(panel):
            overbought = pd.Series(self.config.rsi_overbought, index=rsi.index)
            oversold   = pd.Series(self.config.rsi_oversold,   index=rsi.index)
            return [
                mpf.make_addplot(rsi,        panel=panel, color="#9467bd", width=1.2, ylabel="RSI"),
                mpf.make_addplot(rsi_ma,     panel=panel, color="#ebff0e", width=0.6),
                mpf.make_addplot(overbought, panel=panel, color="#d62728", width=0.8, linestyle="dashdot"),
                mpf.make_addplot(oversold,   panel=panel, color="#00aa00", width=0.8, linestyle="dashdot"),
            ]
        return make, [("RSI", rsi), ("MA", rsi_ma)]

    def kd_diff_subplot(self, diff: pd.Series):
        def make(panel):
            pos   = diff.where(diff > 0)
            neg   = diff.where(diff < 0)
            zero  = diff.where(diff == 0)
            upper = pd.Series(10,  index=diff.index)
            lower = pd.Series(-10, index=diff.index)
            return [
                mpf.make_addplot(pos,   panel=panel, type="bar", color="#00aa00", ylabel="K-D"),
                mpf.make_addplot(neg,   panel=panel, type="bar", color="#d62728"),
                mpf.make_addplot(zero,  panel=panel, type="bar", color="#ffdd00"),
                mpf.make_addplot(upper, panel=panel, color="#838383", width=0.8, linestyle="dashdot"),
                mpf.make_addplot(lower, panel=panel, color="#838383", width=0.8, linestyle="dashdot"),
            ]
        return make, [("K-D", diff)]

    # ------------------------------------------------------------------
    # Overlay builders
    # ------------------------------------------------------------------

    @staticmethod
    def trade_signal_overlays(df: pd.DataFrame, trades: pd.DataFrame) -> list:
        long_entry  = pd.Series(float("nan"), index=df.index)
        long_exit   = pd.Series(float("nan"), index=df.index)
        short_entry = pd.Series(float("nan"), index=df.index)
        short_exit  = pd.Series(float("nan"), index=df.index)

        for _, row in trades.iterrows():
            start_dt = pd.to_datetime(row["Start Time"])
            end_dt   = pd.to_datetime(row["End Time"])
            if start_dt in df.index:
                if row["Position"] == "Long":
                    long_entry[start_dt]  = df["Low"][start_dt]  * 0.993
                else:
                    short_entry[start_dt] = df["High"][start_dt] * 1.007
            if end_dt in df.index:
                if row["Position"] == "Long":
                    long_exit[end_dt]  = df["High"][end_dt] * 1.007
                else:
                    short_exit[end_dt] = df["Low"][end_dt]  * 0.993

        return [
            mpf.make_addplot(long_entry,  panel=0, type="scatter", markersize=120, marker="^", color="#00e676"),
            mpf.make_addplot(long_exit,   panel=0, type="scatter", markersize=100, marker="x", color="#00e676"),
            mpf.make_addplot(short_entry, panel=0, type="scatter", markersize=120, marker="v", color="#ff4444"),
            mpf.make_addplot(short_exit,  panel=0, type="scatter", markersize=100, marker="x", color="#ff4444"),
        ]
