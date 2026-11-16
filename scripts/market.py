# 公开行情与看板 JSON 抓取。只用免费网页接口，不使用任何密钥或账号。
# 主源：腾讯财经日 K / 东财基金净值；东财行情、新浪为后备。
from __future__ import annotations

import datetime as dt
import json
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, ".cache")
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
# 东财公开行情页自带的 ut 参数，不是密钥，与风部、火部脚本相同。
EM_UT = "fa5fd1943c7b386f172d6893dbfba10b"


def get(url, headers=None, timeout=15, tries=3, encoding="utf-8"):
    last = None
    req_headers = dict(UA)
    if headers:
        req_headers.update(headers)
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=req_headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            if encoding == "gbk":
                return raw.decode("gbk", "ignore")
            return raw.decode("utf-8", "ignore")
        except Exception as exc:
            last = exc
            time.sleep(0.4 * (i + 1))
    raise RuntimeError("请求失败 %s: %s" % (url, last))


def get_json(url, **kwargs):
    text = get(url, **kwargs)
    # 去掉可能的 JSONP 包裹
    text = text.strip()
    if text.startswith("jsonpgz(") and text.endswith(");"):
        text = text[8:-2]
    elif text.startswith("jsonpgz(") and text.endswith(")"):
        text = text[8:-1]
    return json.loads(text)


# 无 TTL 时本地缓存会停在过期行情（曾卡在 2026-11-13）。GitHub Actions 每次干净跑，但仍统一过期。
CACHE_TTL_SEC = 6 * 3600
BJ = dt.timezone(dt.timedelta(hours=8))


def _beijing_today():
    return dt.datetime.now(BJ).date().isoformat()


def _cache_dates(obj):
    dates = []
    bars = obj.get("bars") if isinstance(obj, dict) else None
    if isinstance(bars, dict):
        dates.extend(str(k) for k in bars.keys())
        for v in bars.values():
            if isinstance(v, dict) and v.get("date"):
                dates.append(str(v["date"]))
    series = obj.get("series") if isinstance(obj, dict) else None
    if isinstance(series, dict):
        dates.extend(str(k) for k in series.keys())
    return dates


def cache_get(key, max_age=CACHE_TTL_SEC):
    path = os.path.join(CACHE, key)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
    except Exception:
        return None
    ts = obj.get("_cached_at") if isinstance(obj, dict) else None
    if not isinstance(ts, (int, float)):
        return None  # 旧缓存没有时间戳，一律视为过期，避免卡在 11-13
    if time.time() - float(ts) > max_age:
        return None
    dates = _cache_dates(obj)
    if dates and max(dates) < _beijing_today():
        return None
    return obj


def cache_put(key, obj):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, key)
    tmp = path + ".tmp"
    payload = dict(obj)
    payload["_cached_at"] = time.time()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)


def fetch_board_docs(sources):
    """下载四个看板公开 data/*.json。"""
    docs, errors = {}, []
    for sid, cfg in (sources or {}).items():
        base = cfg["base"].rstrip("/")
        pack = {}
        for name in cfg.get("files") or []:
            url = base + "/" + name
            try:
                pack[name.replace(".json", "")] = get_json(url)
            except Exception as exc:
                errors.append("%s/%s: %s" % (sid, name, exc))
        docs[sid] = pack
    return docs, errors


def qq_symbol(code, market=None, kind=None):
    code = str(code or "").strip()
    if kind == "hk_stock" or market == "hk" or (len(code) == 5 and code.isdigit()):
        return "hk" + code.zfill(5)
    if market == "sh":
        return "sh" + code
    if market == "sz":
        return "sz" + code
    if market == "bj":
        return "bj" + code
    if code.startswith(("43", "82", "83", "87", "88", "92", "4", "8")):
        return "bj" + code
    if code.startswith(("5", "6", "9")) and not code.startswith("15"):
        return "sh" + code
    return "sz" + code


def em_secid(code, market=None, kind=None):
    code = str(code or "").strip()
    if kind == "hk_stock" or market == "hk" or (len(code) == 5 and code.isdigit()):
        return "116." + code.zfill(5)
    if market == "sh":
        return "1." + code
    if market == "sz":
        return "0." + code
    if market == "bj":
        return "0." + code
    if code.startswith(("43", "82", "83", "87", "88", "92", "4", "8", "0", "1", "2", "3")):
        return "0." + code
    return "1." + code


def _bars_from_rows(rows):
    """rows: date, open, close, high, low。补 prev。"""
    out, prev = [], None
    for row in rows:
        if not row or len(row) < 3:
            continue
        date = str(row[0])[:10]
        try:
            o = float(row[1])
            c = float(row[2])
            h = float(row[3]) if len(row) > 3 and row[3] not in (None, "") else max(o, c)
            l = float(row[4]) if len(row) > 4 and row[4] not in (None, "") else min(o, c)
        except (TypeError, ValueError):
            continue
        if o <= 0 or c <= 0:
            continue
        rec = {"date": date, "open": o, "close": c, "high": h, "low": l, "prev": prev}
        out.append(rec)
        prev = c
    return out


def _fetch_qq_kline(symbol, lmt=80):
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq" % (symbol, lmt)
    data = get_json(url, headers={"Referer": "https://gu.qq.com/"})
    node = ((data.get("data") or {}).get(symbol)) or {}
    raw = node.get("qfqday") or node.get("day") or []
    rows = []
    for x in raw:
        if len(x) >= 5:
            rows.append([x[0], x[1], x[2], x[3], x[4]])
    return _bars_from_rows(rows), "tencent-kline"


def _fetch_em_kline(secid, lmt=80, end="20500101"):
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s"
        "&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55&klt=101&fqt=1&end=%s&lmt=%d&ut=%s"
        % (secid, end, lmt, EM_UT)
    )
    data = get_json(url)
    lines = ((data.get("data") or {}).get("klines")) or []
    rows = []
    for line in lines:
        p = line.split(",")
        if len(p) >= 3:
            rows.append(p[:5])
    return _bars_from_rows(rows), "eastmoney-kline"


def _fetch_sina_kline(symbol, lmt=80):
    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData?symbol=%s&scale=240&ma=no&datalen=%d" % (symbol, lmt)
    )
    data = json.loads(get(url, headers={"Referer": "https://finance.sina.com.cn/"}))
    rows = []
    for r in data or []:
        rows.append([r.get("day"), r.get("open"), r.get("close"), r.get("high"), r.get("low")])
    return _bars_from_rows(rows), "sina-kline"


def fetch_kline(code, market=None, kind=None, lmt=80):
    key = "kline-%s-%s-%s.json" % (kind or "x", market or "x", code)
    cached = cache_get(key)
    if cached and cached.get("bars"):
        return cached["bars"], cached.get("source") or "cache"
    errors = []
    symbol = qq_symbol(code, market, kind)
    loaders = [
        lambda: _fetch_qq_kline(symbol, lmt),
        lambda: _fetch_em_kline(em_secid(code, market, kind), lmt),
    ]
    if kind != "hk_stock" and market != "hk":
        loaders.append(lambda: _fetch_sina_kline(symbol, lmt))
    for loader in loaders:
        try:
            bars, src = loader()
            if len(bars) >= 2:
                cache_put(key, {"source": src, "bars": bars})
                return bars, src
        except Exception as exc:
            errors.append(str(exc))
    return [], "; ".join(errors) if errors else "无日K"


def fetch_fund_nav(code, pages=4):
    key = "fund-%s.json" % code
    cached = cache_get(key)
    if cached and cached.get("bars"):
        return cached["bars"], cached.get("source") or "cache"
    rows = []
    try:
        for page in range(1, pages + 1):
            url = (
                "https://api.fund.eastmoney.com/f10/lsjz?fundCode=%s&pageIndex=%d&pageSize=20"
                % (code, page)
            )
            data = get_json(url, headers={"Referer": "https://fundf10.eastmoney.com/"})
            lst = ((data.get("Data") or {}).get("LSJZList")) or []
            if not lst:
                break
            for x in lst:
                day = (x.get("FSRQ") or "")[:10]
                dw = x.get("DWJZ")
                lj = x.get("LJJZ")
                if not day or dw in (None, ""):
                    continue
                nav = float(dw)
                acc = float(lj) if lj not in (None, "") else nav
                rows.append([day, nav, acc])
        rows.sort(key=lambda r: r[0])
        # 基金按公布净值成交：开盘、收盘都用单位净值；累计净值用于标注分红缺口。
        bars, prev = [], None
        for day, nav, acc in rows:
            bars.append({
                "date": day, "open": nav, "close": nav, "high": nav, "low": nav,
                "prev": prev, "acc": acc,
            })
            prev = nav
        if len(bars) >= 2:
            cache_put(key, {"source": "eastmoney-lsjz", "bars": bars})
            return bars, "eastmoney-lsjz"
    except Exception as exc:
        return [], "lsjz 失败: %s" % exc
    return [], "lsjz 无净值"


def fetch_spot_fx():
    """腾讯即期汇率，仅作历史序列缺失时的后备。"""
    text = get("https://qt.gtimg.cn/q=fxHKDCNY", encoding="gbk")
    # 样例：v_fxHKDCNY="...~中间价~..."
    parts = text.split("~")
    if len(parts) > 3:
        return float(parts[3]), "tencent-fx-spot"
    raise RuntimeError("即期汇率解析失败")


def fetch_fx_history(lmt=80):
    """尽量取人民币兑港币日线；失败则只用即期。"""
    key = "fx-HKDCNY.json"
    cached = cache_get(key)
    if cached and cached.get("series"):
        return cached["series"], cached.get("source") or "cache"
    candidates = [
        ("qq", "hkHKDCNY"),
        ("em", "133.HKDCNY"),
        ("em", "119.HKDCNY"),
        ("em", "100.HKDCNY"),
    ]
    errors = []
    for typ, sym in candidates:
        try:
            if typ == "qq":
                bars, src = _fetch_qq_kline(sym, lmt)
            else:
                bars, src = _fetch_em_kline(sym, lmt)
            series = {b["date"]: b["close"] for b in bars if b.get("close")}
            if series:
                cache_put(key, {"source": src, "series": series})
                return series, src
        except Exception as exc:
            errors.append("%s:%s %s" % (typ, sym, exc))
    try:
        spot, src = fetch_spot_fx()
        return {"_spot": spot}, src
    except Exception as exc:
        errors.append(str(exc))
        return {}, "; ".join(errors)


def fx_on(series, day):
    if not series:
        return None
    if day in series:
        return series[day]
    earlier = [d for d in series.keys() if d != "_spot" and d <= day]
    if earlier:
        return series[max(earlier)]
    if "_spot" in series:
        return series["_spot"]
    return None


def fetch_calendar(lmt=80):
    bars, src = fetch_kline("000001", "sh", "index", lmt=lmt)
    days = [b["date"] for b in bars]
    return days, src


def _index_candidates(spec):
    out = []
    for item in spec.get("symbols") or []:
        ident = item.get("id") or ""
        label = item.get("label") or ident
        if ident.startswith("em:"):
            out.append(("em", ident[3:], label))
        elif ident.startswith("qq:"):
            out.append(("qq", ident[3:], label))
    return out


def fetch_index(spec, lmt=80):
    errors = []
    for typ, sym, label in _index_candidates(spec):
        try:
            if typ == "em":
                bars, src = _fetch_em_kline(sym, lmt)
            else:
                bars, src = _fetch_qq_kline(sym, lmt)
            series = {b["date"]: b["close"] for b in bars if b.get("close")}
            if len(series) >= 2:
                is_tr = "H00300" in (sym or "") or "H00300" in (label or "")
                # 价格指数的说明里可能提到「全收益」，不能据此判定
                if "价格指数" in (label or ""):
                    is_tr = False
                return series, src, label, is_tr, []
        except Exception as exc:
            errors.append("%s %s: %s" % (typ, sym, exc))
    return {}, None, None, False, errors


def bars_to_map(bars):
    return {b["date"]: b for b in bars or []}


def load_prices(instruments, lmt=80):
    """instruments: list of {code, kind, market, name}。返回 prices[code]=bar_map, sources, gaps。"""
    prices, sources, gaps = {}, set(), []

    def job(inst):
        code, kind = inst["code"], inst.get("kind")
        if kind == "fund":
            bars, src = fetch_fund_nav(code)
        else:
            bars, src = fetch_kline(code, inst.get("market"), kind, lmt=lmt)
        return inst, bars, src

    if not instruments:
        return prices, [], []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(job, inst) for inst in instruments]
        for fut in as_completed(futs):
            inst, bars, src = fut.result()
            code = inst["code"]
            if bars and len(bars) >= 2:
                prices[code] = bars_to_map(bars)
                if src and src != "cache":
                    sources.add(src)
            else:
                gaps.append({
                    "code": code,
                    "name": inst.get("name") or code,
                    "kind": inst.get("kind"),
                    "reason": "行情或净值缺失: %s" % (src or "未知"),
                })
    return prices, sorted(sources), gaps
