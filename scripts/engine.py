# 大鹅基金总账户模拟引擎。虚拟资金，不接券商，不下单。
# 口径：收盘出信号，次日开盘成交（基金用次日单位净值）；佣金万 3、滑点 0.1%；
# A 股股票卖出计 0.05% 印花税；A 股/ETF/场外基金 T+1；涨停买不进、跌停卖不出则顺延；
# 卖出资金当日可用。
from __future__ import annotations

from boards import holdings_asof

EPS = 1e-9


def round_cash(value):
    return int(float(value) * 100 + 0.5) / 100.0


def round_px(value, kind):
    decimals = {"fund": 4, "etf": 3, "hk_stock": 3}.get(kind, 2)
    fac = 10 ** decimals
    return int(float(value) * fac + 0.5) / fac


def lot_of(code, kind):
    if kind == "fund":
        return 0.01
    if kind == "hk_stock":
        return 1
    if str(code).startswith(("688", "689")):
        return 200
    return 100


def limit_pct(code, name, kind):
    if kind not in ("ashare_stock", "etf"):
        return None
    name = (name or "").upper()
    if "ST" in name or "退" in name:
        return 0.05
    if kind == "etf":
        return 0.10
    if str(code).startswith(("300", "301", "688", "689")):
        return 0.20
    if str(code).startswith(("83", "87", "43", "82", "88", "92")):
        return 0.30
    return 0.10


def limit_prices(prev, code, name, kind):
    pct = limit_pct(code, name, kind)
    if pct is None or prev is None:
        return None, None
    return round_px(prev * (1 + pct), kind), round_px(prev * (1 - pct), kind)


def trading_sessions(calendar):
    seen, out = set(), []
    for raw in calendar or []:
        day = str(raw or "")[:10]
        if len(day) == 10 and day not in seen:
            seen.add(day)
            out.append(day)
    out.sort()
    return out


def next_trading_day(calendar, day):
    current = str(day or "")[:10]
    for session in trading_sessions(calendar):
        if session > current:
            return session
    return None


def prev_trading_day(calendar, day):
    current = str(day or "")[:10]
    prev = None
    for session in trading_sessions(calendar):
        if session >= current:
            return prev
        prev = session
    return prev


def is_quarter_end(calendar, day):
    sessions = trading_sessions(calendar)
    if day not in sessions:
        return False
    month = int(day[5:7])
    if month not in (3, 6, 9, 12):
        return False
    nxt = next_trading_day(sessions, day)
    if nxt is None:
        # 日历若在季月中途结束，不能当成季末。
        return False
    return nxt[5:7] != day[5:7] or nxt[:4] != day[:4]


def lot_sellable(kind, buy_date, session, calendar):
    buy = str(buy_date or "")[:10]
    day = str(session or "")[:10]
    if not buy or not day:
        return False
    if kind == "hk_stock":
        return day >= buy
    if day <= buy:
        return False
    sessions = trading_sessions(calendar)
    if day not in sessions:
        return False
    nxt = next_trading_day(sessions, buy)
    return bool(nxt and day >= nxt)


def applies_stamp(kind, costs):
    return kind in (costs.get("stamp_applies_to") or ["ashare_stock"])


def t_plus(kind, fill_cfg):
    return kind in (fill_cfg.get("t_plus_sell") or ["ashare_stock", "etf", "fund"])


class Lot:
    def __init__(self, shares, cost, buy_date):
        self.shares = shares
        self.cost = cost
        self.buy_date = buy_date


class Book:
    def __init__(self, sid, cash):
        self.id = sid
        self.cash = round_cash(cash)
        self.positions = {}  # code -> shares
        self.lots = {}       # code -> [Lot]
        self.meta = {}       # code -> {name, kind, market}
        self.units = cash    # 时间加权净值的份额，建账时 nav=1
        self.nav = 1.0
        self.equity = round_cash(cash)

    def shares(self, code):
        return self.positions.get(code, 0.0)

    def sellable(self, code, day, calendar, kind):
        total = 0.0
        for lot in self.lots.get(code) or []:
            if lot_sellable(kind, lot.buy_date, day, calendar):
                total += lot.shares
        return total


def _quote(prices, code, day):
    return (prices.get(code) or {}).get(day) or {}


def _fx(fx_series, day):
    if not fx_series:
        return 1.0
    if day in fx_series:
        return fx_series[day]
    earlier = [d for d in fx_series if d != "_spot" and d <= day]
    if earlier:
        return fx_series[max(earlier)]
    if "_spot" in fx_series:
        return fx_series["_spot"]
    return 1.0


def _ccy_factor(meta, fx_series, day):
    if (meta or {}).get("kind") == "hk_stock" or (meta or {}).get("market") == "hk":
        rate = _fx(fx_series, day)
        return rate if rate else 1.0
    return 1.0


def _mark_price(quote, field, fallback="close"):
    if not quote:
        return None
    px = quote.get(field)
    if px is None:
        px = quote.get(fallback)
    return px


def sleeve_equity(book, prices, fx_series, day, field="close"):
    total = book.cash
    gap = False
    for code, shares in list(book.positions.items()):
        quote = _quote(prices, code, day)
        px = _mark_price(quote, field)
        if px is None:
            # 沿用最近一次收盘，并记缺口
            hist = prices.get(code) or {}
            earlier = [d for d in hist if d <= day]
            if earlier:
                px = _mark_price(hist[max(earlier)], "close")
                gap = True
            else:
                gap = True
                continue
        factor = _ccy_factor(book.meta.get(code), fx_series, day)
        total += shares * px * factor
    return round_cash(total), gap


def _fill_px(open_px, side, kind, costs):
    slip = float(costs.get("slippage") or 0.0)
    if side == "buy":
        return round_px(open_px * (1 + slip), kind)
    return round_px(open_px * (1 - slip), kind)


def _buy(book, code, shares, open_px, day, costs, since, trades, reason, fx_series=None):
    meta = book.meta.get(code) or {"name": code, "kind": "ashare_stock"}
    kind = meta.get("kind") or "ashare_stock"
    fill = _fill_px(open_px, "buy", kind, costs)
    factor = _ccy_factor(meta, fx_series, day)
    notional = round_cash(fill * shares * factor)
    commission = round_cash(notional * float(costs.get("commission") or 0.0))
    cost = round_cash(notional + commission)
    if cost - book.cash > 0.05:
        raise AssertionError("买入将导致现金为负 %s %s cost=%s cash=%s" % (book.id, code, cost, book.cash))
    book.cash = round_cash(book.cash - cost)
    book.positions[code] = book.shares(code) + shares
    per = (notional + commission) / shares if shares else 0.0
    book.lots.setdefault(code, []).append(Lot(shares, per, day))
    note = reason or "按目标权重买入"
    if since and since != day:
        note = "涨停开盘买不进，自 %s 顺延" % since
    trades.append({
        "date": day, "sleeve": book.id, "code": code, "name": meta.get("name") or code,
        "side": "buy", "shares": shares, "open": open_px, "price": fill,
        "notional": notional, "commission": commission, "stamp": 0.0,
        "slippage": round_cash(abs(fill - open_px) * shares * factor),
        "note": note, "kind": kind, "fx": factor,
    })
    return trades[-1]


def _sell(book, code, shares, open_px, day, costs, since, trades, reason, calendar, fx_series=None):
    meta = book.meta.get(code) or {"name": code, "kind": "ashare_stock"}
    kind = meta.get("kind") or "ashare_stock"
    fill = _fill_px(open_px, "sell", kind, costs)
    factor = _ccy_factor(meta, fx_series, day)
    notional = round_cash(fill * shares * factor)
    commission = round_cash(notional * float(costs.get("commission") or 0.0))
    stamp = round_cash(notional * float(costs.get("stamp_sell") or 0.0)) if applies_stamp(kind, costs) else 0.0
    book.cash = round_cash(book.cash + notional - commission - stamp)
    have = book.shares(code)
    left = have - shares
    if left <= 1e-10:
        book.positions.pop(code, None)
    else:
        book.positions[code] = left
    remain = shares
    lots = book.lots.get(code) or []
    while remain > EPS and lots:
        lot = lots[0]
        if not lot_sellable(kind, lot.buy_date, day, calendar):
            break
        take = min(remain, lot.shares)
        lot.shares -= take
        remain -= take
        if lot.shares <= EPS:
            lots.pop(0)
    note = reason or "调出或降权卖出"
    if since and since != day:
        note = "跌停开盘卖不出，自 %s 顺延" % since
    trades.append({
        "date": day, "sleeve": book.id, "code": code, "name": meta.get("name") or code,
        "side": "sell", "shares": shares, "open": open_px, "price": fill,
        "notional": notional, "commission": commission, "stamp": stamp,
        "slippage": round_cash(abs(open_px - fill) * shares * factor),
        "note": note, "kind": kind, "fx": factor,
    })
    return trades[-1]


def _round_lot(shares, lot):
    if shares <= EPS:
        return 0.0
    if lot <= 0:
        return max(0.0, shares)
    n = int((shares + 1e-12) // lot)
    val = n * lot
    # 基金手数是 0.01，去掉二进制浮点尾巴
    if lot < 1:
        return round(val, 8)
    return val


def rebalance_sleeve(book, day, target, prices, fx_series, costs, fill_cfg, calendar, blocked, trades, reason):
    """把该部持仓调向 target（code -> 权重百分数）。返回 still_blocked dict。"""
    equity, _gap = sleeve_equity(book, prices, fx_series, day, field="open")
    blocks = {}
    plan = {}
    names_target = target or {}
    for h in names_target.values() if False else []:
        pass
    # target: {code: {weight, name, kind, market}}
    for code, spec in (target or {}).items():
        book.meta[code] = {
            "name": spec.get("name") or code,
            "kind": spec.get("kind") or "ashare_stock",
            "market": spec.get("market"),
        }
        quote = _quote(prices, code, day)
        open_px = _mark_price(quote, "open")
        kind = book.meta[code]["kind"]
        factor = _ccy_factor(book.meta[code], fx_series, day)
        if open_px is None or open_px <= 0:
            plan[code] = {"shares": None, "open": None, "weight": spec["weight"]}
            continue
        fill = _fill_px(open_px, "buy", kind, costs)
        unit = fill * factor
        weight = float(spec.get("weight") or 0.0)
        raw = 0.0 if unit <= 0 or weight <= 0 else equity * (weight / 100.0) / unit
        plan[code] = {"shares": _round_lot(raw, lot_of(code, kind)), "open": open_px, "weight": weight}

    # 先卖
    for code in sorted(set(book.positions) | set(plan)):
        have = book.shares(code)
        if code in plan and plan[code]["shares"] is None:
            if have:
                blocks[code] = "开盘价或净值缺失"
            continue
        want = 0.0 if code not in plan else plan[code]["shares"]
        if have <= want + EPS:
            continue
        sell_shares = have - want
        kind = (book.meta.get(code) or {}).get("kind") or "ashare_stock"
        lot = lot_of(code, kind)
        if want > EPS:
            sell_shares = _round_lot(sell_shares, lot)
        if sell_shares <= EPS:
            continue
        sellable = book.sellable(code, day, calendar, kind) if t_plus(kind, fill_cfg) or kind == "hk_stock" else have
        if kind == "hk_stock":
            sellable = have  # T+0
        tradable = min(sell_shares, sellable)
        if want > EPS:
            tradable = _round_lot(tradable, lot)
        if tradable <= EPS:
            blocks[code] = "T+1锁定不可卖"
            continue
        quote = _quote(prices, code, day)
        open_px, prev = _mark_price(quote, "open"), quote.get("prev")
        name = (book.meta.get(code) or {}).get("name") or ""
        if open_px is None:
            blocks[code] = "开盘价或净值缺失"
            continue
        up, down = limit_prices(prev, code, name, kind)
        if down is not None and open_px <= down + EPS:
            blocks[code] = "跌停开盘卖不出"
            blocked.setdefault(code, day)
            continue
        _sell(book, code, tradable, open_px, day, costs, blocked.get(code), trades, reason, calendar, fx_series)
        blocked.pop(code, None)
        if sell_shares > tradable + EPS:
            blocks[code] = "T+1锁定不可卖"

    # 再买
    for code in sorted(plan):
        item = plan[code]
        if item["shares"] is None:
            blocks[code] = "开盘价或净值缺失"
            continue
        have = book.shares(code)
        need = item["shares"] - have
        kind = (book.meta.get(code) or {}).get("kind") or "ashare_stock"
        need = _round_lot(need, lot_of(code, kind))
        if need <= EPS:
            continue
        quote = _quote(prices, code, day)
        open_px, prev = _mark_price(quote, "open"), quote.get("prev")
        name = (book.meta.get(code) or {}).get("name") or ""
        if open_px is None:
            blocks[code] = "开盘价或净值缺失"
            continue
        up, down = limit_prices(prev, code, name, kind)
        if up is not None and open_px + EPS >= up:
            blocks[code] = "涨停开盘买不进"
            blocked.setdefault(code, day)
            continue
        factor = _ccy_factor(book.meta.get(code), fx_series, day)
        fill = _fill_px(open_px, "buy", kind, costs)
        unit = round_cash(fill * factor * (1 + float(costs.get("commission") or 0.0)))
        if unit <= 0:
            continue
        power = max(0.0, book.cash)
        affordable = power / unit if unit else 0.0
        affordable = _round_lot(affordable, lot_of(code, kind))
        take = min(need, affordable)
        # 二次检查：手续费按四舍五入到分后不得把现金打成负数
        while take > EPS:
            fill = _fill_px(open_px, "buy", kind, costs)
            notional = round_cash(fill * take * factor)
            commission = round_cash(notional * float(costs.get("commission") or 0.0))
            if notional + commission <= power + 0.005:
                break
            take = _round_lot(take - lot_of(code, kind), lot_of(code, kind))
        if take <= EPS:
            blocks[code] = "现金不足"
            continue
        _buy(book, code, take, open_px, day, costs, blocked.get(code), trades, reason, fx_series)
        blocked.pop(code, None)
    return blocks


def _target_map(holdings):
    return {h["code"]: dict(h) for h in holdings or []}


def _holdings_key(target):
    return tuple(sorted((str(c), round(float(spec.get("weight") or 0.0), 4)) for c, spec in (target or {}).items()))


def _strategic_weights(rules):
    return {sid: float(cfg["target"]) for sid, cfg in rules["sleeves"].items()}


def _band_of(rules, sid):
    return float(rules["sleeves"][sid].get("band") or 0.05)


def _outside_band(weights, rules):
    offenders = []
    for sid, cfg in rules["sleeves"].items():
        target = float(cfg["target"])
        band = float(cfg.get("band") or 0.05)
        w = weights.get(sid, 0.0)
        if w < target - band - 1e-12 or w > target + band + 1e-12:
            offenders.append(sid)
    return offenders


def _apply_drawdown_targets(strategic, dd_cfg, peak_nav, nav, state):
    """返回 (active_targets, events)。state 记录已触发档位与恢复水位。"""
    if not (dd_cfg or {}).get("enabled"):
        return dict(strategic), []
    events = []
    levels = sorted(dd_cfg.get("levels") or [], key=lambda x: float(x["drawdown"]))
    drawdown = 0.0 if peak_nav <= 0 else max(0.0, 1.0 - nav / peak_nav)
    active = dict(strategic)
    triggered = state.setdefault("triggered", [])
    for lvl in levels:
        th = float(lvl["drawdown"])
        if drawdown + 1e-12 < th:
            continue
        action = lvl.get("action") or "warn"
        key = "dd-%.2f" % th
        if key not in triggered:
            triggered.append(key)
            events.append({
                "type": "risk",
                "level": th,
                "action": action,
                "drawdown": drawdown,
                "reason": lvl.get("note") or ("回撤达到 %.0f%%" % (th * 100)),
            })
            if action == "reduce":
                state["trough"] = nav
                state["restore_at"] = nav + (peak_nav - nav) * 0.5
                state["reduced"] = True
        if action == "reduce":
            for sid, w in (lvl.get("targets") or {}).items():
                active[sid] = float(w)
            leftover = 1.0 - sum(active.values())
            state["cash_target"] = max(0.0, leftover)
    if state.get("reduced") and state.get("restore_at") is not None and nav + 1e-12 >= state["restore_at"]:
        events.append({
            "type": "risk",
            "action": "restore",
            "reason": "净值已收复一半回撤，恢复战略权重",
            "drawdown": drawdown,
        })
        state["reduced"] = False
        state["triggered"] = []
        state["restore_at"] = None
        state["cash_target"] = 0.0
        return dict(strategic), events
    return active, events


def _transfer_units(book, cash_delta):
    """大类再平衡引起的资金进出，按当前 nav 调整份额，保持时间加权净值连续。"""
    if abs(cash_delta) < 0.005 or book.nav <= 0:
        book.cash = round_cash(book.cash + cash_delta)
        return
    book.units = book.units + cash_delta / book.nav
    if book.units < 1e-8:
        book.units = 1e-8
    book.cash = round_cash(book.cash + cash_delta)


def transfer_capital(books, hub_cash, day, targets, prices, fx_series, costs, fill_cfg, calendar, blocked, trades):
    """按 active 大类权重调各部资本。超配的先按权重比例减仓，现金进总账，再补给低配部。"""
    equities = {}
    total = hub_cash
    for sid, book in books.items():
        eq, _ = sleeve_equity(book, prices, fx_series, day, field="open")
        equities[sid] = eq
        total += eq
    total = round_cash(total)
    if total <= 0:
        return hub_cash, {}
    # 先把超配部降到目标
    for sid, book in books.items():
        want = round_cash(total * float(targets.get(sid) or 0.0))
        have = equities[sid]
        if have <= want + 0.5:
            continue
        ratio = want / have if have else 1.0
        # 按比例下调目标持仓（保持品种结构）
        scaled = {}
        eq_close_proxy = have
        # 用当前持仓权重 * ratio 作为新目标，剩余变现金后上缴
        for code, shares in list(book.positions.items()):
            meta = book.meta.get(code) or {"kind": "ashare_stock", "name": code}
            quote = _quote(prices, code, day)
            px = _mark_price(quote, "open") or _mark_price(quote, "close")
            if px is None:
                continue
            factor = _ccy_factor(meta, fx_series, day)
            w = 0.0 if eq_close_proxy <= 0 else (shares * px * factor) / eq_close_proxy * 100.0
            scaled[code] = {**meta, "weight": w * ratio, "code": code}
        rebalance_sleeve(
            book, day, scaled, prices, fx_series, costs, fill_cfg, calendar,
            blocked.setdefault(sid, {}), trades, "大类再平衡减仓",
        )
        new_eq, _ = sleeve_equity(book, prices, fx_series, day, field="open")
        give = round_cash(new_eq - want)
        # 减仓后现金应有富余；若仍超，把现金上缴到目标。
        excess = round_cash(book.cash if give > book.cash else max(0.0, new_eq - want))
        # 实际可上缴现金
        payable = min(book.cash, round_cash(max(0.0, new_eq - want)))
        if payable > 0:
            _transfer_units(book, -payable)
            hub_cash = round_cash(hub_cash + payable)
        equities[sid], _ = sleeve_equity(book, prices, fx_series, day, field="open")

    # 再给低配部补现金（不在这里买股票，留给各部持仓再平衡）
    total = hub_cash + sum(equities.values())
    for sid, book in books.items():
        want = round_cash(total * float(targets.get(sid) or 0.0))
        have, _ = sleeve_equity(book, prices, fx_series, day, field="open")
        need = round_cash(want - have)
        if need <= 0.5:
            continue
        give = min(need, hub_cash)
        if give > 0:
            _transfer_units(book, give)
            hub_cash = round_cash(hub_cash - give)
    return hub_cash, {}


def _rebase_index(series, start):
    if not series:
        return {}, None, None
    earlier_or_start = [d for d in series if d <= start]
    later = [d for d in series if d >= start]
    if start in series:
        base_day = start
    elif earlier_or_start:
        base_day = max(earlier_or_start)
    elif later:
        base_day = min(later)
    else:
        return {}, None, None
    base = series.get(base_day)
    if not base:
        return {}, None, None
    out = {}
    for d in sorted(d for d in series if d >= start or d == base_day):
        if d >= start:
            out[d] = series[d] / base
    if start in series:
        out[start] = 1.0
    return out, base_day, base


def simulate(rules, events_by_sleeve, prices, calendar, fx_series=None, indexes=None):
    """从 start_date 起模拟。start_date 为 None 时不产生成交和净值。"""
    start = rules.get("start_date")
    if not start:
        return not_started(rules, reason="start_date 为空，未建账")
    sessions = trading_sessions(calendar)
    if not sessions:
        return not_started(rules, reason="交易日历缺失，未产生净值")
    first = start if start in sessions else next_trading_day(sessions, prev_trading_day(sessions, start) or start)
    if first is None or first < start and start not in sessions:
        # start 若是休市日，取其后第一个交易日为建账日
        first = next_trading_day(sessions, start) if start not in sessions else start
    if first is None:
        return not_started(rules, reason="建账日之后没有交易日，未产生净值")
    asof = sessions[-1]
    days = [d for d in sessions if first <= d <= asof]
    if not days:
        return not_started(rules, reason="建账日至数据截止日之间没有交易日")

    costs = rules.get("costs") or {}
    fill_cfg = rules.get("fill") or {}
    initial = float(rules.get("initial_cash") or 0.0)
    strategic = _strategic_weights(rules)
    sleeve_ids = list(strategic.keys())
    books = {}
    for sid, w in strategic.items():
        books[sid] = Book(sid, initial * w)
    hub_cash = 0.0
    holding_target = {sid: {} for sid in sleeve_ids}
    # 建账日收盘才下首次建仓信号，开盘时各部仍是现金
    blocked = {sid: {} for sid in sleeve_ids}
    trades, logs, nav_rows, gaps = [], [], [], []
    dd_state = {}
    peak = 1.0
    max_dd = 0.0
    pending_macro = False
    pending_sleeve = {sid: False for sid in sleeve_ids}
    capital_targets = dict(strategic)
    cash_target = 0.0

    def total_equity(day, field="close"):
        s = hub_cash
        g = False
        parts = {}
        for sid, book in books.items():
            eq, gap = sleeve_equity(book, prices, fx_series, day, field=field)
            parts[sid] = eq
            s += eq
            g = g or gap
        return round_cash(s), parts, g

    def mark_nav(day, gap):
        nonlocal peak, max_dd
        total, parts, _ = total_equity(day, "close")
        nav = 0.0 if initial <= 0 else total / initial
        if nav > peak:
            peak = nav
        dd = 0.0 if peak <= 0 else nav / peak - 1.0
        if dd < max_dd:
            max_dd = dd
        weights = {sid: (0.0 if total <= 0 else parts[sid] / total) for sid in sleeve_ids}
        for sid, book in books.items():
            book.equity = parts[sid]
            if book.units > 0:
                book.nav = parts[sid] / book.units
        row = {
            "date": day,
            "nav": nav,
            "equity": total,
            "hub_cash": hub_cash,
            "drawdown": dd,
            "peak_nav": peak,
            "price_gap": gap,
            "sleeves": {},
        }
        for sid in sleeve_ids:
            row["sleeves"][sid] = {
                "equity": parts[sid],
                "weight": weights[sid],
                "nav": books[sid].nav,
                "cash": books[sid].cash,
            }
        nav_rows.append(row)
        return row

    for i, day in enumerate(days):
        # 1) 开盘：先做大类资金再平衡，再按各部目标持仓成交
        if i > 0 and (pending_macro or any(pending_sleeve.values()) or any(blocked[s] for s in sleeve_ids)):
            if pending_macro:
                hub_cash, _ = transfer_capital(
                    books, hub_cash, day, capital_targets, prices, fx_series,
                    costs, fill_cfg, calendar, blocked, trades,
                )
                logs.append({
                    "date": day, "type": "rebalance", "status": "filled",
                    "reason": "按大类目标权重再平衡（次日开盘成交）",
                    "targets": dict(capital_targets),
                })
                pending_macro = False
            for sid, book in books.items():
                if pending_sleeve[sid] or blocked[sid]:
                    rebalance_sleeve(
                        book, day, holding_target[sid], prices, fx_series, costs, fill_cfg,
                        calendar, blocked[sid], trades, "按正式名单调仓",
                    )
                    if blocked[sid]:
                        logs.append({
                            "date": day, "type": "sleeve_trade", "sleeve": sid,
                            "status": "deferred",
                            "reason": "；".join("%s %s" % (c, r) for c, r in blocked[sid].items()),
                        })
                    else:
                        pending_sleeve[sid] = False

        # 2) 收盘估值
        _, _, gap = total_equity(day, "close")
        if gap:
            gaps.append({"date": day, "reason": "部分持仓缺当日价格，已沿用最近收盘"})
        row = mark_nav(day, gap)
        nav = row["nav"]
        weights = {sid: row["sleeves"][sid]["weight"] for sid in sleeve_ids}

        # 3) 回撤降仓（可配置，默认关闭）
        capital_targets, risk_events = _apply_drawdown_targets(
            strategic, rules.get("drawdown_delever") or {}, peak, nav, dd_state,
        )
        cash_target = 1.0 - sum(capital_targets.values())
        for ev in risk_events:
            ev["date"] = day
            ev["status"] = "signal"
            logs.append(ev)
            if ev.get("action") in ("reduce", "restore"):
                pending_macro = True

        # 4) 正式名单：建账日及之后的名单变化，收盘后发信号
        for sid in sleeve_ids:
            official = holdings_asof(events_by_sleeve.get(sid) or [], day)
            wanted = _target_map(official)
            # 建账日：用当时已公布的正式名单作为初始持仓目标
            if day == first and wanted and not holding_target[sid]:
                holding_target[sid] = wanted
                pending_sleeve[sid] = True
                logs.append({
                    "date": day, "type": "sleeve_trade", "sleeve": sid, "status": "signal",
                    "reason": "建账，按当日正式名单次日开盘建仓",
                    "holdings": [{"code": h["code"], "name": h.get("name"), "weight": h["weight"]} for h in official],
                })
            elif day == first and not wanted:
                holding_target[sid] = {}
                # 空仓也是正式状态（例如四王留空）
                logs.append({
                    "date": day, "type": "sleeve_trade", "sleeve": sid, "status": "signal",
                    "reason": "建账时正式名单为空，该部保持现金",
                    "holdings": [],
                })
            elif day > first:
                evs = [e for e in (events_by_sleeve.get(sid) or []) if e["date"] == day]
                if evs and _holdings_key(wanted) != _holdings_key(holding_target[sid]):
                    holding_target[sid] = wanted
                    pending_sleeve[sid] = True
                    ev = evs[-1]
                    logs.append({
                        "date": day, "type": "sleeve_trade", "sleeve": sid, "status": "signal",
                        "reason": ev.get("reason") or "；".join(ev.get("changes") or []) or "正式名单更新",
                        "changes": ev.get("changes") or [],
                        "holdings": [{"code": h["code"], "name": h.get("name"), "weight": h["weight"]} for h in official],
                    })

        # 5) 季末检查或越界触发
        offenders = _outside_band(weights, rules)
        quarter = is_quarter_end(sessions, day)
        rb_cfg = rules.get("rebalance") or {}
        if (rb_cfg.get("quarter_end_check") and quarter) or (rb_cfg.get("band_trigger") and offenders):
            pending_macro = True
            reason = "季末检查，拉回目标权重" if quarter else (
                "权重越界：" + "、".join(
                    "%s %.1f%%（目标 %.0f%%±%.0f）" % (
                        rules["sleeves"][s]["name"],
                        weights[s] * 100,
                        rules["sleeves"][s]["target"] * 100,
                        _band_of(rules, s) * 100,
                    ) for s in offenders
                )
            )
            logs.append({
                "date": day, "type": "rebalance", "status": "signal",
                "reason": reason, "offenders": offenders, "quarter_end": quarter,
                "weights": weights, "targets": dict(capital_targets),
            })

    last = nav_rows[-1] if nav_rows else None
    # 指数定基
    bench = {}
    bench_gaps = []
    for key, series in (indexes or {}).items():
        rebased = {}
        if series:
            rebased, base_day, _base = _rebase_index(series, first)
            if base_day and base_day != first:
                bench_gaps.append("%s 建账日无数据，改从 %s 定基" % (key, base_day))
        bench[key] = rebased
    gaps.extend({"reason": g} for g in bench_gaps)

    # 贡献：各部权益相对各自初始资金的增减 / 总初始
    contrib = {}
    if last:
        for sid in sleeve_ids:
            start_eq = initial * strategic[sid]
            contrib[sid] = 0.0 if initial <= 0 else (last["sleeves"][sid]["equity"] - start_eq) / initial

    return {
        "status": "ok",
        "started": True,
        "start_date": first,
        "asof": asof,
        "initial_cash": initial,
        "nav": last["nav"] if last else 1.0,
        "equity": last["equity"] if last else initial,
        "hub_cash": hub_cash,
        "peak_nav": peak,
        "drawdown": last["drawdown"] if last else 0.0,
        "max_drawdown": max_dd,
        "curves": nav_rows,
        "benchmarks": bench,
        "contribution": contrib,
        "sleeves": last["sleeves"] if last else {},
        "holding_target": {sid: list(holding_target[sid].values()) for sid in sleeve_ids},
        "capital_targets": capital_targets,
        "cash_target": cash_target,
        "trades": trades,
        "logs": logs,
        "gaps": gaps,
        "pending_macro": pending_macro,
        "pending_sleeve": pending_sleeve,
        "blocked": blocked,
        "strategic": strategic,
    }


def not_started(rules, reason="start_date 为空，未建账"):
    strategic = _strategic_weights(rules) if rules.get("sleeves") else {}
    return {
        "status": "not_started",
        "started": False,
        "reason": reason,
        "start_date": rules.get("start_date"),
        "initial_cash": float(rules.get("initial_cash") or 0.0),
        "nav": None,
        "equity": None,
        "hub_cash": 0.0,
        "peak_nav": None,
        "drawdown": None,
        "max_drawdown": None,
        "curves": [],
        "benchmarks": {},
        "contribution": {},
        "sleeves": {},
        "holding_target": {},
        "capital_targets": strategic,
        "cash_target": 0.0,
        "trades": [],
        "logs": [],
        "gaps": [],
        "pending_macro": False,
        "pending_sleeve": {},
        "blocked": {},
        "strategic": strategic,
    }
