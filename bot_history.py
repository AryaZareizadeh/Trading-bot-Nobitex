"""Durable bot history, separate from the current strategy's runtime counters."""
import hashlib
import json


def history_ids(bot):
    # Runner IDs remain stable even when a combined bot is changed to a single bot.
    return [bot.id, f'{bot.id}:long', f'{bot.id}:short']


def simulation_records(bot):
    records = {}
    for runner in bot.execution_bots():
        if runner.cfg.mode != 'simulation':
            continue
        for index, trade in enumerate(runner.completed_trades):
            identity = [runner.id, runner.history_epoch, trade.get('Position'),
                        trade.get('Start Time'), trade.get('Trade #', index)]
            key = hashlib.sha256(json.dumps(identity, default=str).encode()).hexdigest()
            records[key] = {'id': key, 'bot_id': runner.id, 'source': 'simulation',
                            'symbol': runner.cfg.symbol.upper(), 'strategy': runner.cfg.strategy_name,
                            'timeframe': runner.cfg.timeframe, 'side': getattr(runner, 'side', None),
                            'status': 'closed' if index < runner.closed_trade_count else 'open',
                            'trade': dict(trade)}
    return records


def save_simulation_history(bot, conn):
    for key, record in simulation_records(bot).items():
        bot._store.write(conn, 'bot_trades', key, record)


def detail_history(bot):
    ids = history_ids(bot)
    events = bot._store.list_for_bots('audit_events', ids) if bot._store else {}
    # Preserve legacy snapshot logs that predate durable audit records.
    seen = {(e['bot_id'], e['time'], e['event'], e['detail']) for e in events.values()}
    combined = list(events.values())
    for runner in [bot, *[r for r in bot.execution_bots() if r is not bot]]:
        for item in runner.activity_log:
            signature = (runner.id, item['time'], item['event'], item['detail'])
            if signature not in seen:
                combined.append(dict(item, bot_id=runner.id))
                seen.add(signature)
    if bot.created_at and not any(e['event'] == 'bot_created' and e['bot_id'] == bot.id for e in combined):
        combined.append({'bot_id': bot.id, 'time': bot.created_at.isoformat(),
                         'event': 'bot_created', 'detail': 'Creation time from saved bot metadata'})
    for event in combined:
        if event['bot_id'] != bot.id:
            event['side'] = event['bot_id'].rsplit(':', 1)[-1]
    trades = bot._store.list_for_bots('bot_trades', ids) if bot._store else {}
    trades.update(simulation_records(bot))
    orders = bot._store.list_for_bots('orders', ids) if bot._store else {}
    positions = bot._store.list_for_bots('closed_positions', ids) if bot._store else {}
    return {'history_events': sorted(combined, key=lambda e: e['time'], reverse=True),
            'trade_history': sorted(trades.values(), key=lambda t: str(t['trade'].get('Start Time', '')), reverse=True),
            'order_ledger': sorted(orders.values(), key=lambda o: o.get('created_at', ''), reverse=True),
            'closed_exchange_positions': [p['position'] for p in positions.values()]}
