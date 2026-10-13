"""Final settlement evidence shared by reconciliation and live lifecycle paths."""
from datetime import datetime


def position_is_settled(position, *, in_past=False):
    status = position.get("status")
    if status in ("Closed", "Canceled"):
        return True
    if status not in ("Expired", "Liquidated"):
        return False
    # System liquidation/expiry can remain active while its orders settle.
    # Nobitex defines closedAt as final settlement and past as settled history.
    if in_past:
        return True
    try:
        closed = datetime.fromisoformat(str(position.get("closedAt", "")).replace("Z", "+00:00"))
        return closed.tzinfo is not None
    except (TypeError, ValueError):
        return False


async def settled_position(client, position):
    """Return confirmed final state, using history only if evidence is absent."""
    if position_is_settled(position):
        return position
    if position.get("status") not in ("Expired", "Liquidated"):
        return None
    import asyncio
    for past in await asyncio.to_thread(client.positions, "past"):
        if (str(past.get("id")) == str(position.get("id"))
                and past.get("srcCurrency") == position.get("srcCurrency")
                and past.get("dstCurrency") == position.get("dstCurrency")
                and past.get("side") == position.get("side")
                and position_is_settled(past, in_past=True)):
            return past
    return None
