# 成交台账与风控仪表。只读模拟结果，不下单，不改目标、档位或费用。
from __future__ import annotations

NEAR_OF_LIMIT = 0.8
BAND_NEAR = 0.01
_BLANK_INDUSTRY = ("其他", "未知", "-", "—")


def _clean_industry(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in _BLANK_INDUSTRY:
        return None
    return text


def _cents(value):
    return int(round(float(value or 0) * 100))


def _magnitude(drawdown):
    """引擎回撤是 nav/peak - 1，零或负数。正数不当成亏损。"""
    if drawdown is None:
        return None
    val = float(drawdown)
    if val >= 0:
        return 0.0
    return -val


def limit_status(weight, limit, near_ratio=NEAR_OF_LIMIT, missing=False):
    if missing or weight is None:
        return "缺价"
    limit = float(limit)
    near = limit * float(near_ratio)
    if float(weight) + 1e-12 >= limit:
        return "超过"
    if float(weight) + 1e-12 >= near:
        return "接近"
    return "未接近"


def classify_band(weight, target, band, near=BAND_NEAR):
    target = float(target or 0.0)
    band = float(band or 0.0)
    low = target - band
    high = target + band
    out = {
        "target": target,
        "band": band,
        "low": low,
        "high": high,
        "weight": None if weight is None else float(weight),
        "action": "仅展示",
    }
    if weight is None:
        out.update({
            "inside": None,
            "status": "无权重",
            "distance_to_low": None,
            "distance_to_high": None,
        })
        return out
    weight = float(weight)
    distance_to_low = weight - low
    distance_to_high = high - weight
    if weight < low - 1e-12:
        status, inside = "低于下限", False
    elif weight > high + 1e-12:
        status, inside = "高于上限", False
    else:
        inside = True
        room = min(distance_to_low, distance_to_high)
        status = "接近边界" if room <= float(near) + 1e-12 else "区间内"
    out.update({
        "inside": inside,
        "status": status,
        "distance_to_low": distance_to_low,
        "distance_to_high": distance_to_high,
    })
    return out


def drawdown_board(drawdown, levels, enabled=True, applied_targets=None, strategic=None, dd_state=None):
    mag = _magnitude(drawdown)
    rows = []
    ordered = sorted(levels or [], key=lambda x: float(x.get("drawdown") or 0.0))
    for lvl in ordered:
        th = float(lvl.get("drawdown") or 0.0)
        if mag is None:
            distance, reached = None, False
        else:
            distance = th - mag
            reached = mag + 1e-12 >= th
        targets = dict(lvl.get("targets") or {})
        rows.append({
            "drawdown": th,
            "action": lvl.get("action") or "warn",
            "note": lvl.get("note"),
            "targets": targets or None,
            "distance": distance,
            "reached": reached,
        })
    active = None
    for row in rows:
        if row["reached"]:
            active = {
                "drawdown": row["drawdown"],
                "action": row["action"],
                "note": row["note"],
            }
    state = dd_state or {}
    return {
        "current_signed": None if drawdown is None else float(drawdown),
        "magnitude": mag,
        "enabled": bool(enabled),
        "levels": rows,
        "active": active,
        "reduced": bool(state.get("reduced")),
        "restore_nav": state.get("restore_at"),
        "applied_targets": {k: float(v) for k, v in (applied_targets or {}).items()},
        "strategic_targets": {k: float(v) for k, v in (strategic or {}).items()},
        "display_only": True,
    }


def _intent_side(shares, target_weight, eps):
    held = float(shares or 0.0)
    tw = float(target_weight or 0.0)
    if held <= eps and tw > eps:
        return "buy"
    if tw <= eps and held > eps:
        return "sell"
    if held > eps and tw > eps:
        return "adjust"
    return None


def _hk_rate(fx_series, day):
    if not fx_series or not day:
        return None
    if day in fx_series and fx_series[day]:
        return float(fx_series[day])
    earlier = [d for d in fx_series if d != "_spot" and str(d) <= str(day) and fx_series[d]]
    if earlier:
        return float(fx_series[max(earlier)])
    if fx_series.get("_spot"):
        return float(fx_series["_spot"])
    return None


def _sleeve_name(rules, sid):
    cfg = (rules.get("sleeves") or {}).get(sid) or {}
    return cfg.get("board") or cfg.get("name") or sid


def _lock_status(kind, locked, sellable, eps):
    if kind == "hk_stock":
        return "T+0 当日可卖"
    if not kind:
        return "无法判断"
    if locked > eps and sellable > eps:
        return "部分锁定"
    if locked > eps:
        return "T+1锁定"
    return "可卖"


def compose(
    rules, started, asof, books, trades, holding_target, blocked, block_reasons,
    pending_sleeve, pending_macro, last_signal, last_macro, capital_targets, strategic,
    prices, fx_series, calendar, nav_row, dd_state,
):
    from engine import EPS, lot_sellable, next_trading_day, round_cash, t_plus, _quote

    rules = rules or {}
    books = books or {}
    trades = trades or []
    holding_target = holding_target or {}
    blocked = blocked or {}
    block_reasons = block_reasons or {}
    pending_sleeve = pending_sleeve or {}
    last_signal = last_signal or {}
    capital_targets = capital_targets or {}
    strategic = strategic or {}
    prices = prices or {}
    fill_cfg = rules.get("fill") or {}
    sleeve_ids = list((rules.get("sleeves") or {}).keys())
    nav_row = nav_row or None
    equity = None if not nav_row else nav_row.get("equity")
    weight_of = {}
    if nav_row:
        for sid, part in (nav_row.get("sleeves") or {}).items():
            weight_of[sid] = part.get("weight")

    filled = []
    comm = stamp = slip = 0
    for trade in trades:
        row = dict(trade)
        row["status"] = "filled"
        c = _cents(trade.get("commission"))
        s = _cents(trade.get("stamp"))
        p = _cents(trade.get("slippage"))
        row["fee_total"] = (c + s + p) / 100.0
        filled.append(row)
        comm += c
        stamp += s
        slip += p
    fees = {
        "commission": comm / 100.0,
        "stamp": stamp / 100.0,
        "slippage": slip / 100.0,
        "total": (comm + stamp + slip) / 100.0,
    }

    positions = []
    locks = []
    for sid in sleeve_ids:
        book = books.get(sid)
        if not book:
            continue
        for code, shares in sorted(book.positions.items()):
            if shares <= EPS:
                continue
            meta = book.meta.get(code) or {}
            spec = (holding_target.get(sid) or {}).get(code) or {}
            kind = meta.get("kind") or spec.get("kind")
            name = meta.get("name") or spec.get("name") or code
            industry = _clean_industry(spec.get("industry")) or _clean_industry(meta.get("industry"))
            source = None
            if industry:
                source = spec.get("industry_source") or meta.get("industry_source") or "given"
            quote = _quote(prices, code, asof) if asof else {}
            close = quote.get("close") if quote else None
            price_gap = close is None
            market = meta.get("market") or spec.get("market")
            fx_gap = False
            rate = 1.0
            if kind == "hk_stock" or market == "hk":
                rate = _hk_rate(fx_series, asof)
                if not rate:
                    fx_gap = True
            market_value = None
            if close is not None and not fx_gap:
                market_value = round_cash(float(shares) * float(close) * float(rate))
            positions.append({
                "sleeve": sid,
                "sleeve_name": _sleeve_name(rules, sid),
                "code": code,
                "name": name,
                "kind": kind,
                "shares": shares,
                "industry": industry,
                "industry_source": source,
                "close": None if price_gap else float(close),
                "price_date": None if price_gap else asof,
                "price_gap": bool(price_gap),
                "fx_gap": bool(fx_gap),
                "market_value": market_value,
            })

            locked = 0.0
            sellable = 0.0
            lot_rows = []
            t1 = bool(kind) and kind != "hk_stock" and t_plus(kind, fill_cfg)
            for lot in book.lots.get(code) or []:
                if lot.shares <= EPS:
                    continue
                if not kind:
                    is_locked, sell_on = None, None
                elif not t1:
                    is_locked, sell_on = False, lot.buy_date
                else:
                    is_locked = True if not asof else not lot_sellable(kind, lot.buy_date, asof, calendar)
                    sell_on = next_trading_day(calendar, lot.buy_date)
                if is_locked:
                    locked += lot.shares
                elif is_locked is False:
                    sellable += lot.shares
                lot_rows.append({
                    "buy_date": lot.buy_date,
                    "shares": lot.shares,
                    "locked": is_locked,
                    "sellable_on": sell_on,
                })
            if not kind:
                status = "无法判断"
            else:
                status = _lock_status(kind, locked, sellable, EPS)
            locks.append({
                "sleeve": sid,
                "sleeve_name": _sleeve_name(rules, sid),
                "code": code,
                "name": name,
                "kind": kind,
                "shares": shares,
                "locked_shares": locked,
                "sellable_shares": sellable,
                "status": status,
                "lots": lot_rows,
            })

    pending = []
    notes = []
    if not started:
        notes.append("未建账，没有成交、待成交或持仓。")
    for sid in sleeve_ids:
        book = books.get(sid)
        reasons = block_reasons.get(sid) or {}
        still = blocked.get(sid) or {}
        if still:
            target = holding_target.get(sid) or {}
            for code, since in sorted(still.items()):
                spec = target.get(code) or {}
                meta = (book.meta.get(code) if book else None) or {}
                shares = book.shares(code) if book else 0.0
                pending.append({
                    "status": "deferred",
                    "sleeve": sid,
                    "sleeve_name": _sleeve_name(rules, sid),
                    "code": code,
                    "name": spec.get("name") or meta.get("name") or code,
                    "side": _intent_side(shares, spec.get("weight"), EPS),
                    "since": since,
                    "reason": reasons.get(code) or "顺延，原因未记下",
                    "target_weight": spec.get("weight"),
                    "current_shares": shares,
                    "kind": spec.get("kind") or meta.get("kind"),
                })
        elif pending_sleeve.get(sid):
            target = holding_target.get(sid) or {}
            has_pos = bool(book and any(sh > EPS for sh in book.positions.values()))
            if not target and not has_pos:
                notes.append("%s正式名单为空，保持现金，没有待成交。" % _sleeve_name(rules, sid))
            else:
                sig = last_signal.get(sid) or {}
                reason = "已发信号尚未成交，待次日开盘。股数与价格待开盘确定，不估算。"
                if sig.get("reason"):
                    reason = reason + "信号：" + sig["reason"]
                since = sig.get("date") or asof
                seen = set()
                for code, spec in sorted(target.items()):
                    seen.add(code)
                    shares = book.shares(code) if book else 0.0
                    side = _intent_side(shares, spec.get("weight"), EPS)
                    if side is None:
                        continue
                    meta = (book.meta.get(code) if book else None) or {}
                    pending.append({
                        "status": "signal",
                        "sleeve": sid,
                        "sleeve_name": _sleeve_name(rules, sid),
                        "code": code,
                        "name": spec.get("name") or meta.get("name") or code,
                        "side": side,
                        "since": since,
                        "reason": reason,
                        "target_weight": spec.get("weight"),
                        "current_shares": shares,
                        "kind": spec.get("kind") or meta.get("kind"),
                    })
                if book:
                    for code, shares in sorted(book.positions.items()):
                        if code in seen or shares <= EPS:
                            continue
                        meta = book.meta.get(code) or {}
                        pending.append({
                            "status": "signal",
                            "sleeve": sid,
                            "sleeve_name": _sleeve_name(rules, sid),
                            "code": code,
                            "name": meta.get("name") or code,
                            "side": "sell",
                            "since": since,
                            "reason": reason,
                            "target_weight": 0,
                            "current_shares": shares,
                            "kind": meta.get("kind"),
                        })
    if pending_macro:
        info = last_macro or {}
        pending.append({
            "status": "macro",
            "sleeve": None,
            "sleeve_name": "总账户",
            "code": None,
            "name": "大类再平衡",
            "side": None,
            "since": info.get("date") or asof,
            "reason": "已发信号尚未成交，待次日开盘。不估算各标的股数。%s" % (info.get("reason") or ""),
            "target_weight": None,
            "current_shares": None,
            "kind": None,
            "targets": {k: float(v) for k, v in (info.get("targets") or capital_targets).items()},
        })
    pending.sort(key=lambda row: (str(row.get("since") or ""), str(row.get("sleeve") or ""), str(row.get("code") or ""), row.get("status") or ""))

    empty = []
    if started:
        for sid in sleeve_ids:
            book = books.get(sid)
            held = bool(book and any(sh > EPS for sh in book.positions.values()))
            if not held:
                cfg = (rules.get("sleeves") or {}).get(sid) or {}
                empty.append({"id": sid, "name": cfg.get("name") or sid, "board": cfg.get("board")})

    ledger_gaps = []
    if any(t.get("kind") == "hk_stock" for t in trades) or any(p.get("kind") == "hk_stock" for p in positions):
        ledger_gaps.append({
            "reason": "港股印花税未单列。明细中的印花税只含 A 股股票卖出 0.05%；港股成交印花税为 0 是口径缺口，不是已计算的香港印花税。",
        })

    limits = rules.get("position_limits") or {}
    single_max = float(limits.get("single_stock_max_of_total") or 0.08)
    ind_max = float(limits.get("industry_lookthrough_max_of_total") or 0.30)
    single_rows = []
    for pos in positions:
        if pos["sleeve"] != "fire":
            continue
        missing = bool(pos["price_gap"] or pos["fx_gap"] or pos["market_value"] is None or not equity)
        weight = None
        if not missing:
            weight = pos["market_value"] / float(equity)
        single_rows.append({
            "sleeve": "fire",
            "code": pos["code"],
            "name": pos["name"],
            "kind": pos["kind"],
            "market_value": pos["market_value"],
            "weight_of_total": weight,
            "limit": single_max,
            "status": limit_status(weight, single_max, missing=missing),
            "price_gap": bool(pos["price_gap"] or pos["fx_gap"]),
            "action": "仅展示，不调仓",
        })

    groups = {}
    unclassified = []
    incomplete = []
    for pos in positions:
        brief = {
            "sleeve": pos["sleeve"],
            "sleeve_name": pos["sleeve_name"],
            "code": pos["code"],
            "name": pos["name"],
            "kind": pos["kind"],
            "market_value": pos["market_value"],
        }
        if not pos.get("industry"):
            brief["reason"] = "行业分类缺失"
            unclassified.append(brief)
            continue
        if pos["market_value"] is None:
            brief["industry"] = pos["industry"]
            brief["reason"] = "有行业标签但缺少截止日价格，不计入占比"
            incomplete.append(brief)
            continue
        bucket = groups.setdefault(pos["industry"], {"market_value": 0.0, "positions": []})
        bucket["market_value"] = round_cash(bucket["market_value"] + pos["market_value"])
        bucket["positions"].append({
            "sleeve": pos["sleeve"],
            "code": pos["code"],
            "name": pos["name"],
            "source": pos.get("industry_source"),
        })
    ind_rows = []
    for name, bucket in sorted(groups.items()):
        weight = None if not equity else bucket["market_value"] / float(equity)
        ind_rows.append({
            "industry": name,
            "market_value": bucket["market_value"],
            "weight_of_total": weight,
            "limit": ind_max,
            "status": limit_status(weight, ind_max, missing=weight is None),
            "positions": bucket["positions"],
            "action": "仅报告，不强制调仓",
        })

    lagging = []
    for pos in positions:
        if not (pos["price_gap"] or pos["fx_gap"]):
            continue
        if pos["price_gap"] and pos["fx_gap"]:
            why = "缺少截止日收盘价和港币汇率"
        elif pos["fx_gap"]:
            why = "缺少港币汇率"
        else:
            why = "缺少截止日收盘价"
        lagging.append({
            "sleeve": pos["sleeve"], "code": pos["code"], "name": pos["name"], "reason": why,
        })

    risk_gaps = []
    if lagging:
        risk_gaps.append({
            "reason": "%d 只持仓缺少截止日价格或汇率，单票与行业占比留空，不沿用旧价填数。" % len(lagging),
        })
    if unclassified:
        risk_gaps.append({
            "reason": "%d 只持仓缺少行业分类，未计入行业占比，也不归入「其他」。" % len(unclassified),
        })
    if incomplete:
        risk_gaps.append({
            "reason": "%d 只持仓有行业标签但缺少截止日价格，该部分不计入行业占比。" % len(incomplete),
        })
    if any(p.get("kind") == "etf" for p in positions):
        risk_gaps.append({
            "reason": "ETF 只使用看板 sector 标签，没有成分股穿透明细；没有标签的 ETF 不计入行业占比。",
        })
    if any(p.get("kind") == "fund" for p in positions):
        risk_gaps.append({
            "reason": "场外基金没有行业持仓明细，不把投资风格当成行业，不计入行业占比。",
        })

    dd_cfg = rules.get("drawdown_delever") or {}
    signed = None if not nav_row else nav_row.get("drawdown")
    bands = []
    for sid, cfg in (rules.get("sleeves") or {}).items():
        band = classify_band(
            weight_of.get(sid) if started else None,
            cfg.get("target") or 0.0,
            cfg.get("band") if cfg.get("band") is not None else 0.05,
        )
        band["id"] = sid
        band["name"] = cfg.get("name") or sid
        band["board"] = cfg.get("board")
        active_target = capital_targets.get(sid)
        band["active_target"] = None if active_target is None else float(active_target)
        bands.append(band)

    risk = {
        "schema": "risk-v1",
        "display_only": True,
        "asof": asof,
        "note": "超线只展示，不改规则，不自动调仓。不模拟融资。",
        "band_note": "区间按战略权重和允许带宽计算。本仪表不因此下单；既有收盘越界再平衡仍只按原规则执行。",
        "drawdown": drawdown_board(
            signed, dd_cfg.get("levels") or [], bool(dd_cfg.get("enabled")),
            capital_targets, strategic, dd_state,
        ),
        "bands": bands,
        "single_stock": {
            "scope": "fire",
            "limit": single_max,
            "near_ratio": NEAR_OF_LIMIT,
            "near_at": single_max * NEAR_OF_LIMIT,
            "rows": single_rows,
            "empty": not any(p["sleeve"] == "fire" for p in positions),
            "action": "仅展示，不调仓",
        },
        "industry": {
            "limit": ind_max,
            "near_ratio": NEAR_OF_LIMIT,
            "near_at": ind_max * NEAR_OF_LIMIT,
            "rows": ind_rows,
            "unclassified": unclassified,
            "incomplete": incomplete,
            "method": "只加总已有行业标签且有截止日价格的持仓。股票用看板行业，ETF 用看板板块标签，不是成分股明细。没有标签的不归入其他。",
            "action": "仅报告，不强制调仓",
        },
        "freshness": {
            "asof": asof,
            "updated_at": None,
            "lagging": lagging,
            "note": "时点是已模拟的最后一个交易日收盘。" if started else "未建账，没有净值时点。",
        },
        "gaps": risk_gaps,
    }
    ledger = {
        "schema": "ledger-v1",
        "asof": asof,
        "note": "账本来自本次模拟的成交、信号和持仓，不是子组合旧纸面账户，也不是券商交割。",
        "filled": filled,
        "pending": pending,
        "locks": locks,
        "positions": positions,
        "fees": fees,
        "empty_sleeves": empty,
        "notes": notes,
        "gaps": ledger_gaps,
    }
    return ledger, risk


def extra_gaps(ledger, risk):
    out, seen = [], set()
    for src in (ledger, risk):
        for gap in (src or {}).get("gaps") or []:
            reason = gap.get("reason") if isinstance(gap, dict) else str(gap)
            if reason and reason not in seen:
                seen.add(reason)
                out.append({"reason": reason})
    return out
