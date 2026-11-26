# 解析四个看板公开的 official / history JSON，得到各部「某日收盘后的正式持仓」。
# 不倒推建账日之前的业绩；只把名单和权重整理成时间线，供引擎从建账日起下单。
from __future__ import annotations

SLEEVE_ORDER = ("shan", "fire", "feng", "lin")

KIND_BY_SLEEVE = {"feng": "etf", "lin": "fund", "fire": "ashare_stock"}


def parse_weight(raw):
    if raw is None or raw == "":
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip().replace("%", "")
    if "/" in text:
        a, b = text.split("/", 1)
        denom = float(b)
        return 0.0 if denom == 0 else 100.0 * float(a) / denom
    return float(text)


def infer_kind(code, sleeve, market=None):
    if sleeve == "feng":
        return "etf"
    if sleeve == "lin":
        return "fund"
    code = str(code or "").strip()
    mkt = (market or "").lower()
    if mkt in ("hk", "hkex") or (len(code) == 5 and code.isdigit()):
        return "hk_stock"
    if sleeve == "fire":
        return "ashare_stock"
    if len(code) == 6 and code.startswith(("15", "51", "56", "58")):
        return "etf"
    return "ashare_stock"


def infer_market(code, kind, market=None):
    if market:
        return str(market).lower()
    if kind in ("hk_stock",):
        return "hk"
    if kind == "fund":
        return "fund"
    code = str(code or "").strip()
    if code.startswith(("5", "6", "9")) and not code.startswith(("15",)):
        return "sh"
    if code.startswith(("43", "82", "83", "87", "88", "92", "4", "8")):
        return "bj"
    return "sz"


def industry_fields(raw, source):
    """只搬运来源里已经写明的行业标签。空值、「其他」「未知」不当成行业，也不从名称猜测。"""
    if not isinstance(raw, dict):
        return None
    ind = raw.get("ind")
    if ind is None or str(ind).strip() == "":
        ind = raw.get("industry")
    if ind is None or str(ind).strip() == "":
        ind = raw.get("sector")
    if ind is None:
        return None
    text = str(ind).strip()
    if not text or text in ("其他", "未知", "-", "—"):
        return None
    return {"industry": text, "industry_source": source}


def _item(code, name, weight, sleeve, market=None, extra=None):
    kind = infer_kind(code, sleeve, market)
    rec = {
        "code": str(code).strip(),
        "name": name or str(code).strip(),
        "weight": float(weight or 0.0),
        "kind": kind,
        "market": infer_market(code, kind, market),
    }
    if extra:
        rec.update(extra)
    return rec


def _renorm(items):
    """只修正接近 100% 的四舍五入误差。合计明显低于 100% 时不放大，以便留下现金。"""
    total = sum(x["weight"] for x in items)
    if total <= 0:
        return items
    if 99.0 <= total <= 101.0 and abs(total - 100.0) > 0.15:
        for x in items:
            x["weight"] = x["weight"] * 100.0 / total
    return items


def holdings_asof(events, day):
    """events 已按日期排序。返回 day 当天收盘时已公布的正式持仓（含当日调仓）。"""
    current = []
    for ev in events or []:
        if ev["date"] <= day:
            current = ev.get("holdings") or []
        else:
            break
    return [dict(x) for x in current]


def _append_official(events, official_date, holdings, changes, reason):
    if not official_date or not holdings:
        return events
    if events and events[-1]["date"] == official_date:
        events[-1] = {
            "date": official_date,
            "holdings": holdings,
            "changes": changes or events[-1].get("changes") or [],
            "reason": reason or events[-1].get("reason") or "",
        }
        return events
    if any(ev["date"] == official_date for ev in events):
        return events
    events.append({
        "date": official_date,
        "holdings": holdings,
        "changes": changes or [],
        "reason": reason or "",
    })
    events.sort(key=lambda ev: ev["date"])
    return events


def parse_feng(official, history, latest=None):
    events = []
    for ev in (history or {}).get("events") or []:
        holdings = []
        for h in ev.get("holdings") or []:
            holdings.append(_item(h.get("code"), h.get("name"), parse_weight(h.get("weight")), "feng"))
        events.append({
            "date": ev["date"][:10],
            "holdings": _renorm(holdings),
            "changes": ev.get("changes") or [],
            "reason": ev.get("reason") or "",
        })
    off = official or {}
    date = (off.get("rebalance_date") or "")[:10]
    holdings = []
    scores = off.get("scores") or {}
    latest = latest or {}
    name_by = {}
    for row in latest.get("rows") or []:
        if row.get("code"):
            name_by[str(row["code"])] = row.get("name") or row["code"]
    for ev in (history or {}).get("events") or []:
        for h in ev.get("holdings") or []:
            name_by.setdefault(str(h.get("code")), h.get("name"))
        for h in ev.get("entered") or []:
            name_by.setdefault(str(h.get("code")), h.get("name"))
    sector_by = {}
    for row in (latest or {}).get("rows") or []:
        if row.get("code") and row.get("sector"):
            sector_by[str(row["code"])] = row.get("sector")
    for ev in events:
        for h in ev.get("holdings") or []:
            extra = industry_fields({"sector": sector_by.get(h["code"])}, "board_sector") if sector_by.get(h["code"]) else None
            if extra:
                h.update(extra)
    codes = off.get("codes") or []
    n = len(codes)
    for code in codes:
        extra = industry_fields({"sector": sector_by.get(str(code))}, "board_sector") if sector_by.get(str(code)) else None
        holdings.append(_item(code, name_by.get(str(code), str(code)), 100.0 / n if n else 0.0, "feng", extra=extra))
    return _append_official(events, date, _renorm(holdings), off.get("changes") or [], "正式名单")


def parse_lin(official, history, latest=None):
    book = {}
    events = []
    for ev in (history or {}).get("events") or []:
        for o in ev.get("out") or []:
            book.pop(str(o.get("code")), None)
        for i in ev.get("in") or []:
            book[str(i["code"])] = {
                "code": str(i["code"]),
                "name": i.get("name") or i["code"],
                "weight": parse_weight(i.get("weight")),
                "style": i.get("style"),
            }
        style_w = ev.get("weights") or {}
        if style_w:
            for rec in book.values():
                if rec.get("style") in style_w:
                    rec["weight"] = parse_weight(style_w[rec["style"]])
        holdings = [_item(r["code"], r["name"], r["weight"], "lin") for r in book.values()]
        events.append({
            "date": ev["date"][:10],
            "holdings": _renorm(holdings),
            "changes": ev.get("changes") or [],
            "reason": ev.get("reason") or "",
        })
    off = official or {}
    if not off.get("picks") and latest and (latest.get("official") or {}).get("picks"):
        off = latest["official"]
    date = (off.get("rebalance_date") or "")[:10]
    # 海外补入发生在 11-09，official.rebalance_date 仍可能写 11-06。
    # 若 history 已有更完整、更晚的名单，不要用较早的 official 日期覆盖。
    picks = off.get("picks") or []
    holdings = [_item(p["code"], p.get("name"), parse_weight(p.get("weight")), "lin") for p in picks]
    latest_hist = max((ev["date"] for ev in events), default="")
    if date and date < latest_hist:
        date = latest_hist
    return _append_official(events, date, _renorm(holdings), off.get("changes") or [], "正式名单")


def parse_fire(official, history, latest=None):
    events = []
    for ev in (history or {}).get("events") or []:
        weights = ev.get("weights") or ev.get("items") or []
        holdings = []
        if isinstance(weights, list):
            for h in weights:
                if not isinstance(h, dict) or not h.get("code"):
                    continue
                holdings.append(_item(
                    h["code"], h.get("name"), parse_weight(h.get("weight")), "fire",
                    extra=industry_fields(h, "board_industry"),
                ))
        events.append({
            "date": ev["date"][:10],
            "holdings": _renorm(holdings),
            "changes": ev.get("changes") or [],
            "reason": ev.get("reason") or "",
        })
    off = official or {}
    date = (off.get("rebalance_date") or "")[:10]
    holdings = []
    for h in off.get("items") or []:
        if h.get("code"):
            holdings.append(_item(
                h["code"], h.get("name"), parse_weight(h.get("weight")), "fire",
                extra=industry_fields(h, "board_industry"),
            ))
    for code in off.get("codes") or []:
        if not any(x["code"] == str(code) for x in holdings):
            n = len(off.get("codes") or [])
            holdings.append(_item(code, str(code), 100.0 / n if n else 0.0, "fire"))
    # 四王留空也要记一笔，否则建账后不知道「空仓是正式决定」。
    if date:
        events = _append_official(
            events, date, _renorm(holdings),
            off.get("changes") or ["四王留空"] if not holdings else off.get("changes") or [],
            "正式名单",
        )
        if events and events[-1]["date"] == date and not holdings:
            events[-1]["holdings"] = []
    return events


def parse_shan(official, history, latest=None):
    market_by = {}
    name_by = {}
    for row in (latest or {}).get("rows") or []:
        if row.get("code"):
            market_by[str(row["code"])] = row.get("mkt") or row.get("market")
            name_by[str(row["code"])] = row.get("name") or row["code"]
    events = []
    for ev in (history or {}).get("events") or []:
        holdings = []
        for h in ev.get("weights") or []:
            code = str(h.get("code"))
            holdings.append(_item(
                code, h.get("name") or name_by.get(code),
                parse_weight(h.get("weight")), "shan", market_by.get(code),
                industry_fields(h, "board_ind"),
            ))
        events.append({
            "date": ev["date"][:10],
            "holdings": _renorm(holdings),
            "changes": ev.get("changes") or [],
            "reason": ev.get("reason") or "",
        })
    off = official or {}
    date = (off.get("rebalance_date") or "")[:10]
    holdings = []
    for h in off.get("items") or []:
        code = str(h.get("code"))
        holdings.append(_item(
            code, h.get("name") or name_by.get(code),
            parse_weight(h.get("weight")), "shan", market_by.get(code),
            industry_fields(h, "board_ind"),
        ))
    return _append_official(events, date, _renorm(holdings), off.get("changes") or [], "正式名单")


PARSERS = {"feng": parse_feng, "lin": parse_lin, "fire": parse_fire, "shan": parse_shan}


def parse_all(docs):
    """docs[sleeve] = {official, history, latest}。返回 {sleeve: events}。"""
    out = {}
    for sid in SLEEVE_ORDER:
        pack = docs.get(sid) or {}
        out[sid] = PARSERS[sid](pack.get("official") or {}, pack.get("history") or {}, pack.get("latest"))
    return out


def collect_codes(events_by_sleeve, start_date):
    """建账日及之后可能用到的代码（含建账日已有持仓）。"""
    needed = {sid: [] for sid in SLEEVE_ORDER}
    seen = {sid: set() for sid in SLEEVE_ORDER}
    for sid, events in (events_by_sleeve or {}).items():
        # 建账日要用「当时已公布」的持仓，所以也要收录 start_date 之前最近一次名单里的代码
        last_before = []
        for ev in events:
            if start_date and ev["date"] < start_date:
                last_before = ev.get("holdings") or []
                continue
            bag = ev.get("holdings") or []
            if ev["date"] == start_date:
                last_before = []
            for h in bag:
                code = h["code"]
                if code not in seen[sid]:
                    seen[sid].add(code)
                    needed[sid].append(h)
        for h in last_before:
            if h["code"] not in seen[sid]:
                seen[sid].add(h["code"])
                needed[sid].append(h)
    return needed
