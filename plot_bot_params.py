"""
plot_bot_params.py

Reads a bots JSON file (like the one exported from your trading bot manager)
that lives in the SAME FOLDER as this script, and draws a bar chart for every
custom_params field so you can quickly see how different the bots' settings
are from each other.

Usage:
    1. Put this script and your bots .json file in the same folder.
    2. Run:  python plot_bot_params.py
    3. A window pops up with one small bar chart per parameter, and a PNG
       ("bot_params_comparison.png") is saved next to the script.

It will automatically use a file called "bots.json" if one exists in the
folder. Otherwise it just picks the first .json file it finds there, so you
don't have to rename anything.
"""

import json
import math
from pathlib import Path
import matplotlib.pyplot as plt


def load_bots():
    script_dir = Path(__file__).resolve().parent

    preferred = script_dir / "bots.json"
    if preferred.exists():
        file_path = preferred
    else:
        json_files = sorted(script_dir.glob("*.json"))
        if not json_files:
            raise FileNotFoundError(f"No .json file found in {script_dir}")
        file_path = json_files[0]

    with open(file_path, "r") as f:
        bots = json.load(f)

    print(f"Loaded {len(bots)} bots from {file_path.name}")
    return bots


def short_name(bot):
    """Bot names look like 'XLMUSDT·3654·balanced' - just use the first
    part (the symbol) for chart labels, e.g. 'XLMUSDT'."""
    full_name = bot.get("name", bot.get("id", "bot"))
    return full_name.split("\u00b7")[0]


def plot_params(bots):
    if not bots:
        print("No bots found in the JSON file - nothing to plot.")
        return

    # Collect every custom_params key seen across ALL bots (union), keeping
    # first-seen order. This way it doesn't matter how many bots there are,
    # or if some bots have extra/missing parameters compared to others.
    param_names = []
    for bot in bots:
        for key in bot.get("custom_params", {}):
            if key not in param_names:
                param_names.append(key)

    n = len(param_names)
    if n == 0:
        print("No custom_params found on any bot - nothing to plot.")
        return

    labels = [short_name(bot) for bot in bots]
    colors = plt.cm.tab10.colors
    bar_colors = [colors[j % len(colors)] for j in range(len(bots))]

    cols = min(4, n)
    rows = -(-n // cols)  # ceiling division

    fig, axes = plt.subplots(rows, cols, figsize=(cols * 3.6, rows * 3.2), squeeze=False)
    axes = axes.flatten()

    for i, param in enumerate(param_names):
        ax = axes[i]
        # If a bot doesn't have this parameter, use NaN so the bar is simply
        # skipped instead of crashing the chart.
        values = [bot.get("custom_params", {}).get(param, math.nan) for bot in bots]
        values = [v if v is not None else math.nan for v in values]

        # Horizontal bars: bot names on the y-axis, parameter values on the x-axis.
        ax.barh(labels, values, color=bar_colors)
        ax.invert_yaxis()  # keep bot order top-to-bottom matching the JSON
        ax.set_title(param, fontsize=10)
        ax.set_xlabel("Value", fontsize=8)
        ax.tick_params(axis="x", labelsize=7)
        ax.tick_params(axis="y", labelsize=8)

    # hide any unused subplot slots (e.g. 11 params in a 4-wide grid leaves 1 empty)
    for j in range(n, len(axes)):
        fig.delaxes(axes[j])

    fig.suptitle("Bot Custom Parameter Comparison", fontsize=14)
    fig.tight_layout(rect=[0, 0, 1, 0.96])

    out_path = Path(__file__).resolve().parent / "bot_params_comparison.png"
    # fig.savefig(out_path, dpi=150)
    print(f"Saved chart image to {out_path}")

    plt.show()


if __name__ == "__main__":
    bots = load_bots()
    plot_params(bots)