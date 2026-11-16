#!/usr/bin/env python3
"""读取四个看板公开 JSON，按 config/rules.json 计算总账户，写出 data/summary.json 与 data/changes.json。
先跑自测。start_date 为 null 时不产生成交和净值。
不连接券商，不使用任何密钥。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import boards
import engine
import market
import self_test

BJ = dt.timezone(dt.timedelta(hours=8))
DISCLAIMER = "模拟盘，非投资建议。虚拟资金，不接券商，不动真钱。"


def now_bj():
    return dt.datetime.now(BJ).strftime("%Y-%m-%d %H:%M")


def load_rules():
    path = os.path.join(ROOT, "config", "rules.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(name, obj):
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    path = os.path.join(ROOT, "data", name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return path


def official_snapshot(docs, rules=None):
    """未建账时也可附带各部最新正式名单，只作展示，不生成净值。
    林部展示名单与建仓信号同一公司门槛：不合格或未核实者不作为本机采用的正式持仓。
    公开仓库的官方名单本身不在此改写。"""
    out = {}
    events = boards.parse_all(docs) if docs else {}
    for sid in boards.SLEEVE_ORDER:
        evs = events.get(sid) or []
        last = evs[-1] if evs else None
        holdings = list(last.get("holdings") or []) if last else []
        rec = {
            "asof": last["date"] if last else None,
            "holdings": holdings,
            "changes": last.get("changes") if last else [],
        }
        if sid == "lin" and rules:
            import lin_company_filter
            held, dropped = lin_company_filter.filter_holdings(holdings, rules)
            rec["holdings"] = held
            rec["public_holdings"] = holdings
            if dropped:
                rec["excluded"] = [
                    {"code": h.get("code"), "name": h.get("name"), "weight": h.get("weight"),
                     "company": h.get("company"), "reason": h.get("reason")}
                    for h in dropped
                ]
                rec["note"] = (
                    "公开正式名单仍含下列基金，本机总览已按公司门槛剔除，不作为建仓信号；"
                    "对应权重暂留现金，不放大其余持仓，不强制补入。公开仓库尚未推送。"
                )
        out[sid] = rec
    return out, events


def collect_instruments(events_by_sleeve, start):
    needed = boards.collect_codes(events_by_sleeve, start)
    inst = []
    seen = set()
    for sid, items in needed.items():
        for h in items:
            code = h["code"]
            if code in seen:
                continue
            seen.add(code)
            inst.append(h)
    return inst


def build_summary(rules, result, docs, sources, gaps, board_errors):
    sleeves_cfg = rules["sleeves"]
    started = bool(result.get("started"))
    official, _ = official_snapshot(docs, rules) if docs else ({}, {})
    curves = []
    for row in result.get("curves") or []:
        item = {
            "date": row["date"],
            "nav": row["nav"],
            "drawdown": row["drawdown"],
            "equity": row["equity"],
            "hub_cash": row.get("hub_cash", 0),
            "sleeves": {},
        }
        for sid, part in (row.get("sleeves") or {}).items():
            item["sleeves"][sid] = {
                "nav": part["nav"],
                "weight": part["weight"],
                "equity": part["equity"],
            }
        benches = result.get("benchmarks") or {}
        for key, series in benches.items():
            item[key] = (series or {}).get(row["date"])
        curves.append(item)

    current = result.get("sleeves") or {}
    weights = []
    for sid, cfg in sleeves_cfg.items():
        part = current.get(sid) or {}
        weights.append({
            "id": sid,
            "name": cfg["name"],
            "board": cfg.get("board") or cfg["name"],
            "url": cfg.get("url"),
            "target": cfg["target"],
            "band": cfg.get("band") or 0.05,
            "low": cfg["target"] - (cfg.get("band") or 0.05),
            "high": cfg["target"] + (cfg.get("band") or 0.05),
            "weight": part.get("weight"),
            "equity": part.get("equity"),
            "nav": part.get("nav"),
            "cash": part.get("cash"),
            "contribution": (result.get("contribution") or {}).get(sid),
            "official": official.get(sid),
        })

    all_gaps = list(result.get("gaps") or []) + list(gaps or [])
    for err in board_errors or []:
        all_gaps.append({"reason": "看板公开数据读取失败: %s" % err})

    dd_cfg = rules.get("drawdown_delever") or {}
    return {
        "disclaimer": rules.get("disclaimer") or DISCLAIMER,
        "status": result.get("status") or ("ok" if started else "not_started"),
        "started": started,
        "reason": result.get("reason"),
        "start_date": result.get("start_date") if started else rules.get("start_date"),
        "asof": result.get("asof") if started else None,
        "updated_at": now_bj(),
        "timezone": "Asia/Shanghai",
        "currency": rules.get("currency") or "CNY",
        "initial_cash": rules.get("initial_cash"),
        "nav": result.get("nav") if started else None,
        "equity": result.get("equity") if started else None,
        "hub_cash": result.get("hub_cash") if started else 0,
        "drawdown": result.get("drawdown") if started else None,
        "max_drawdown": result.get("max_drawdown") if started else None,
        "peak_nav": result.get("peak_nav") if started else None,
        "weights": weights,
        "contribution": result.get("contribution") or {},
        "curves": curves,
        "benchmarks": {
            "primary": {
                "id": "hs300tr",
                "name": (rules.get("benchmarks") or {}).get("primary", {}).get("name") or "沪深300全收益",
            },
            "secondary": {
                "id": "equity_fund",
                "name": (rules.get("benchmarks") or {}).get("secondary", {}).get("name") or "中证偏股基金指数",
            },
        },
        "logs": (result.get("logs") or [])[-20:],
        "trades": (result.get("trades") or [])[-40:],
        "links": [{"id": sid, "name": cfg["name"], "board": cfg.get("board"), "url": cfg.get("url")}
                  for sid, cfg in sleeves_cfg.items()],
        "drawdown_delever": {
            "enabled": bool(dd_cfg.get("enabled")),
            "note": dd_cfg.get("note"),
        },
        "rules": {
            "fill": "收盘出信号，次日开盘成交；基金按次日单位净值。",
            "costs": "佣金万 3、滑点 0.1%；A 股股票卖出印花税 0.05%；ETF 与场外基金不收印花税。",
            "t_plus": "A 股、ETF、场外基金 T+1 可卖；港股当日可卖。卖出资金当日可用。",
            "rebalance": "季末检查；任一部偏离目标超过允许区间则拉回目标。",
        },
        "sources": sources,
        "gaps": all_gaps,
        "pending_macro": result.get("pending_macro"),
        "pending_sleeve": result.get("pending_sleeve"),
    }


def todays_changes(result, asof):
    logs = [lg for lg in (result.get("logs") or []) if lg.get("date") == asof]
    trades = [t for t in (result.get("trades") or []) if t.get("date") == asof]
    kinds = []
    if any(lg.get("type") == "sleeve_trade" for lg in logs) or trades:
        kinds.append("子组合调仓")
    if any(lg.get("type") == "rebalance" for lg in logs):
        kinds.append("大类再平衡")
    if any(lg.get("type") == "risk" for lg in logs):
        kinds.append("风控触发")
    return {
        "disclaimer": DISCLAIMER,
        "updated_at": now_bj(),
        "asof": asof,
        "started": bool(result.get("started")),
        "has_changes": bool(logs or trades),
        "kinds": kinds,
        "logs": logs,
        "trades": trades,
    }


def build():
    rules = load_rules()
    start = rules.get("start_date")
    board_errors, docs, sources, gaps = [], {}, [], []
    if not start:
        result = engine.not_started(rules)
        # 未建账也可读各部名单作展示，失败不阻断
        try:
            docs, board_errors = market.fetch_board_docs(rules.get("sources") or {})
            if docs:
                sources.append("四个看板公开 JSON")
        except Exception as exc:
            board_errors.append(str(exc))
        summary = build_summary(rules, result, docs, sources, gaps, board_errors)
        changes = todays_changes(result, None)
        write_json("summary.json", summary)
        write_json("changes.json", changes)
        print("未建账：已写出 data/summary.json（无净值曲线）")
        return summary, changes

    docs, board_errors = market.fetch_board_docs(rules.get("sources") or {})
    if board_errors and not any(docs.values()):
        raise RuntimeError("四个看板公开数据都无法读取: %s" % "; ".join(board_errors))
    sources.append("四个看板公开 JSON")
    events = boards.parse_all(docs)
    instruments = collect_instruments(events, start)
    prices, px_sources, px_gaps = market.load_prices(instruments, lmt=120)
    sources.extend(px_sources)
    gaps.extend(px_gaps)

    calendar, cal_src = market.fetch_calendar(lmt=120)
    if cal_src:
        sources.append(cal_src)
    if not calendar:
        raise RuntimeError("上交所交易日历缺失，拒绝编造净值")

    fx_series, fx_src = market.fetch_fx_history(lmt=120)
    if fx_src:
        sources.append(fx_src)
    if not fx_series:
        gaps.append({"reason": "港币汇率历史缺失，港股持仓无法准确折算"})

    indexes, bench_notes = {}, []
    for key, spec in (("hs300tr", (rules.get("benchmarks") or {}).get("primary")),
                      ("equity_fund", (rules.get("benchmarks") or {}).get("secondary"))):
        if not spec:
            continue
        series, src, label, is_tr, errors = market.fetch_index(spec, lmt=120)
        if series:
            indexes[key] = series
            sources.append(src)
            if key == "hs300tr" and not is_tr:
                gaps.append({
                    "reason": "沪深300全收益暂不可用，已改用 %s。短窗口分红差可忽略，长窗口会低估基准。" % (label or src)
                })
            else:
                bench_notes.append(label)
        else:
            gaps.append({"reason": "基准 %s 缺失: %s" % (spec.get("name"), "; ".join(errors) or "无数据")})

    # 若山部 latest 带有即期汇率而历史没有，补一个 spot
    if (not fx_series or set(fx_series) == {"_spot"}) and docs.get("shan"):
        fx = ((docs["shan"].get("latest") or {}).get("fx") or {}).get("HKDCNY")
        if fx:
            fx_series = dict(fx_series or {})
            fx_series["_spot"] = float(fx)
            sources.append("shan latest.fx")

    result = engine.simulate(rules, events, prices, calendar, fx_series, indexes)
    if result.get("asof") and start and result["asof"] < start:
        gaps.append({"reason": "数据截止日早于建账日"})

    # 火部看板纸面资金仍是 100 万；总账户按 300 万再算一遍，不直接复用 paper.json 的金额。
    fire_paper = (docs.get("fire") or {}).get("paper") or {}
    if fire_paper:
        paper_init = fire_paper.get("initial_cash")
        target_fire = float(rules["initial_cash"]) * float(rules["sleeves"]["fire"]["target"])
        if paper_init and abs(paper_init - target_fire) > 1:
            gaps.append({
                "reason": "火部 paper.json 虚拟资金为 %.0f 元，总账户按目标权重使用 %.0f 元，未按比例放大其持仓，而是按正式名单自行记账。"
                % (paper_init, target_fire)
            })

    summary = build_summary(rules, result, docs, sources, gaps, board_errors)
    changes = todays_changes(result, result.get("asof"))
    write_json("summary.json", summary)
    write_json("changes.json", changes)
    print("建账运行完成：start=%s asof=%s nav=%s trades=%d gaps=%d" % (
        result.get("start_date"), result.get("asof"), result.get("nav"),
        len(result.get("trades") or []), len(summary.get("gaps") or []),
    ))
    return summary, changes


def main():
    print("运行自测…")
    self_test.run_all()
    build()


if __name__ == "__main__":
    main()
