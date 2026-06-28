import threading

_NOTIFY_REAL = {"order_placed", "position_closed"}
_NOTIFY_SIM  = {"position_opened", "trade_closed"}


def _fire(title: str, message: str) -> None:
    try:
        from plyer import notification
        notification.notify(title=title, message=message, app_name="Trading Bot", timeout=6)
    except Exception:
        pass


def notify_if_relevant(mode: str, event: str, bot_label: str, detail: str) -> None:
    relevant = _NOTIFY_REAL if mode == "real" else _NOTIFY_SIM
    if event not in relevant:
        return
    threading.Thread(
        target=_fire,
        args=(f"Trading Bot — {bot_label}", detail),
        daemon=True,
    ).start()
