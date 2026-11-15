#!/usr/bin/env python3
"""用 tests/ 目录下明确标注的合成夹具验证模拟引擎。不访问网络，不写 data/。"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")

import engine
from boards import holdings_asof, parse_weight, parse_lin, parse_feng
from engine import (
    is_quarter_end, lot_of, next_trading_day, not_started, round_cash,
    simulate, trading_sessions,
)


def _bars(seq, start_prev=None):
    """seq: (date, open, close)。纯测试夹具。"""
    out, prev = {}, start_prev
    for date, o, c in seq:
        out[date] = {
            "date": date, "open": float(o), "close": float(c),
            "high": max(float(o), float(c)), "low": min(float(o), float(c)),
            "prev": prev,
        }
        prev = float(c)
    return out


def _load_calendar():
    with open(os.path.join(FIXTURES, "calendar.json"), encoding="utf-8") as f:
        doc = json.load(f)
    assert "SYNTHETIC" in (doc.get("note") or "")
    return doc["sessions"]


def _base_rules(start, sleeves, enabled_dd=False):
    costs = {"commission": 0.0003, "slippage": 0.001, "stamp_sell": 0.0005, "stamp_applies_to": ["ashare_stock"]}
    fill = {
        "signal": "close", "execution": "next_open", "fund_fill": "next_nav",
        "t_plus_sell": ["ashare_stock", "etf", "fund"], "sale_proceeds_same_day": True,
    }
    dd = {
        "enabled": enabled_dd,
        "levels": [
            {"drawdown": 0.10, "action": "warn", "note": "预警"},
            {"drawdown": 0.15, "action": "reduce", "targets": {"fire": 0.15, "feng": 0.15}, "note": "降仓15"},
            {"drawdown": 0.20, "action": "reduce", "targets": {"fire": 0.0, "feng": 0.10}, "note": "降仓20"},
        ],
        "restore": {"rule": "half_drawdown_recovered"},
    }
    return {
        "start_date": start,
        "initial_cash": 1_000_000,
        "sleeves": sleeves,
        "costs": costs,
        "fill": fill,
        "rebalance": {"quarter_end_check": True, "band_trigger": True, "pull_to": "target"},
        "drawdown_delever": dd,
    }


def _two_sleeves():
    return {
        "shan": {"id": "shan", "name": "山部", "target": 0.50, "band": 0.05},
        "fire": {"id": "fire", "name": "火部", "target": 0.50, "band": 0.05},
    }


def _four_sleeves():
    return {
        "shan": {"id": "shan", "name": "山部", "target": 0.30, "band": 0.05},
        "fire": {"id": "fire", "name": "火部", "target": 0.30, "band": 0.05},
        "feng": {"id": "feng", "name": "风部", "target": 0.20, "band": 0.05},
        "lin": {"id": "lin", "name": "林部", "target": 0.20, "band": 0.05},
    }


def test_null_start_date():
    rules = _base_rules(None, _four_sleeves())
    result = simulate(rules, {}, {}, ["2026-11-09", "2026-11-10"])
    assert result["status"] == "not_started" and result["started"] is False
    assert result["curves"] == [] and result["trades"] == []
    assert result["nav"] is None


def test_t_plus_one():
    """11-09 收盘建仓，11-10 开盘买入；当日不可卖，11-11 才可卖。"""
    calendar = ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12"]
    prices = {
        "600000": _bars([
            ("2026-11-09", 10.0, 10.0),
            ("2026-11-10", 10.0, 10.0),
            ("2026-11-11", 10.0, 10.0),
            ("2026-11-12", 10.0, 10.0),
        ], start_prev=10.0),
        "000001": _bars([
            ("2026-11-09", 10.0, 10.0),
            ("2026-11-10", 10.0, 10.0),
            ("2026-11-11", 10.0, 10.0),
            ("2026-11-12", 10.0, 10.0),
        ], start_prev=10.0),
    }
    events = {
        "shan": [{"date": "2026-11-09", "holdings": [
            {"code": "600000", "name": "测试股A", "weight": 100, "kind": "ashare_stock", "market": "sh"}
        ], "changes": ["建仓"], "reason": "建仓"}],
        "fire": [{"date": "2026-11-09", "holdings": [
            {"code": "000001", "name": "测试股B", "weight": 100, "kind": "ashare_stock", "market": "sz"}
        ], "changes": ["建仓"], "reason": "建仓"}],
    }
    rules = _base_rules("2026-11-09", _two_sleeves())
    # 11-10 买入后，当天把名单清空，若 T+1 生效则 11-10 卖不出，11-11 才能卖
    events["shan"].append({"date": "2026-11-10", "holdings": [], "changes": ["清仓"], "reason": "清仓"})
    events["fire"].append({"date": "2026-11-10", "holdings": [], "changes": ["清仓"], "reason": "清仓"})
    result = simulate(rules, events, prices, calendar)
    assert result["started"]
    sells = [t for t in result["trades"] if t["side"] == "sell"]
    buys = [t for t in result["trades"] if t["side"] == "buy"]
    assert buys and all(t["date"] == "2026-11-10" for t in buys), buys
    assert sells, "应在可卖日卖出"
    assert all(t["date"] != "2026-11-10" for t in sells), "买入当日不可卖: %s" % sells
    assert all(t["date"] == "2026-11-11" for t in sells), sells


def test_limit_defer():
    calendar = ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12"]
    # 600000: 11-09 close 10; 11-10 涨停开盘 11.00 买不进；11-11 开盘 10.50 买进
    # 000001: 先买进，再在 11-11 跌停卖不出，11-12 才卖出
    prices = {
        "600000": _bars([
            ("2026-11-09", 10.00, 10.00),
            ("2026-11-10", 11.00, 11.00),
            ("2026-11-11", 10.50, 10.50),
            ("2026-11-12", 10.50, 10.50),
        ], start_prev=10.00),
        "000001": _bars([
            ("2026-11-09", 10.00, 10.00),
            ("2026-11-10", 10.00, 11.00),
            ("2026-11-11", 9.90, 9.90),
            ("2026-11-12", 10.00, 10.00),
        ], start_prev=10.00),
    }
    events = {
        "shan": [{"date": "2026-11-09", "holdings": [
            {"code": "600000", "name": "测试股A", "weight": 100, "kind": "ashare_stock", "market": "sh"}
        ], "changes": ["建仓"], "reason": "建仓"}],
        "fire": [
            {"date": "2026-11-09", "holdings": [
                {"code": "000001", "name": "测试股B", "weight": 100, "kind": "ashare_stock", "market": "sz"}
            ], "changes": ["建仓"], "reason": "建仓"},
            {"date": "2026-11-10", "holdings": [], "changes": ["清仓"], "reason": "清仓"},
        ],
    }
    rules = _base_rules("2026-11-09", _two_sleeves())
    result = simulate(rules, events, prices, calendar)
    shan_buys = [t for t in result["trades"] if t["code"] == "600000" and t["side"] == "buy"]
    assert shan_buys and shan_buys[0]["date"] == "2026-11-11", shan_buys
    assert "涨停" in shan_buys[0]["note"]
    fire_sells = [t for t in result["trades"] if t["code"] == "000001" and t["side"] == "sell"]
    assert fire_sells and fire_sells[0]["date"] == "2026-11-12", fire_sells
    assert "跌停" in fire_sells[0]["note"]
    assert fire_sells[0]["stamp"] > 0


def test_costs():
    calendar = ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12"]
    prices = {
        "600000": _bars([
            ("2026-11-09", 10.0, 10.0),
            ("2026-11-10", 10.0, 10.0),
            ("2026-11-11", 10.0, 10.0),
            ("2026-11-12", 10.0, 10.0),
        ], start_prev=10.0),
        "510300": _bars([
            ("2026-11-09", 4.0, 4.0),
            ("2026-11-10", 4.0, 4.0),
            ("2026-11-11", 4.0, 4.0),
            ("2026-11-12", 4.0, 4.0),
        ], start_prev=4.0),
    }
    events = {
        "shan": [
            {"date": "2026-11-09", "holdings": [
                {"code": "600000", "name": "测试股A", "weight": 100, "kind": "ashare_stock", "market": "sh"}
            ], "changes": [], "reason": "建仓"},
            {"date": "2026-11-10", "holdings": [], "changes": ["清仓"], "reason": "清仓"},
        ],
        "fire": [
            {"date": "2026-11-09", "holdings": [
                {"code": "510300", "name": "测试ETF", "weight": 100, "kind": "etf", "market": "sh"}
            ], "changes": [], "reason": "建仓"},
            {"date": "2026-11-10", "holdings": [], "changes": ["清仓"], "reason": "清仓"},
        ],
    }
    rules = _base_rules("2026-11-09", _two_sleeves())
    result = simulate(rules, events, prices, calendar)
    buy = next(t for t in result["trades"] if t["code"] == "600000" and t["side"] == "buy")
    sell = next(t for t in result["trades"] if t["code"] == "600000" and t["side"] == "sell")
    assert buy["date"] == "2026-11-10"
    assert abs(buy["price"] - 10.0 * 1.001) < 1e-9
    assert abs(buy["commission"] - round_cash(buy["notional"] * 0.0003)) < 1e-9
    assert buy["stamp"] == 0
    assert sell["date"] == "2026-11-11"
    assert abs(sell["price"] - 10.0 * 0.999) < 1e-9
    assert abs(sell["commission"] - round_cash(sell["notional"] * 0.0003)) < 1e-9
    assert abs(sell["stamp"] - round_cash(sell["notional"] * 0.0005)) < 1e-9
    etf_sell = next(t for t in result["trades"] if t["code"] == "510300" and t["side"] == "sell")
    assert etf_sell["stamp"] == 0, "ETF 卖出不应计 A 股印花税"


def test_rebalance_band():
    """两部各 50%，山部股票两日内大涨导致越界，应在越界当日收盘发信号、次日开盘再平衡。"""
    calendar = ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"]
    prices = {
        "600000": _bars([
            ("2026-11-09", 10.0, 10.0),
            ("2026-11-10", 10.0, 13.0),  # +30% close，山部权重升到约 56%
            ("2026-11-11", 13.0, 13.0),
            ("2026-11-12", 13.0, 13.0),
            ("2026-11-13", 13.0, 13.0),
        ], start_prev=10.0),
        "000001": _bars([
            ("2026-11-09", 10.0, 10.0),
            ("2026-11-10", 10.0, 10.0),
            ("2026-11-11", 10.0, 10.0),
            ("2026-11-12", 10.0, 10.0),
            ("2026-11-13", 10.0, 10.0),
        ], start_prev=10.0),
    }
    events = {
        "shan": [{"date": "2026-11-09", "holdings": [
            {"code": "600000", "name": "测试股A", "weight": 100, "kind": "ashare_stock", "market": "sh"}
        ], "changes": [], "reason": "建仓"}],
        "fire": [{"date": "2026-11-09", "holdings": [
            {"code": "000001", "name": "测试股B", "weight": 100, "kind": "ashare_stock", "market": "sz"}
        ], "changes": [], "reason": "建仓"}],
    }
    rules = _base_rules("2026-11-09", _two_sleeves())
    result = simulate(rules, events, prices, calendar)
    signals = [lg for lg in result["logs"] if lg.get("type") == "rebalance" and lg.get("status") == "signal"]
    assert signals, result["logs"]
    assert signals[0]["date"] == "2026-11-10"
    assert "shan" in (signals[0].get("offenders") or [])
    filled = [lg for lg in result["logs"] if lg.get("type") == "rebalance" and lg.get("status") == "filled"]
    assert filled and filled[0]["date"] == "2026-11-11", filled
    last = result["curves"][-1]
    w_shan = last["sleeves"]["shan"]["weight"]
    w_fire = last["sleeves"]["fire"]["weight"]
    assert abs(w_shan - 0.50) < 0.03, (w_shan, w_fire)
    assert abs(w_fire - 0.50) < 0.03, (w_shan, w_fire)


def test_quarter_end():
    calendar = _load_calendar()
    assert is_quarter_end(calendar, "2026-09-30")
    assert not is_quarter_end(calendar, "2026-09-29")
    assert not is_quarter_end(calendar, "2026-11-13")
    prices = {
        "600000": _bars([
            ("2026-09-28", 10.0, 10.0),
            ("2026-09-29", 10.0, 10.2),
            ("2026-09-30", 10.2, 10.3),  # 轻微漂移，未越 5 个点
            ("2026-10-09", 10.3, 10.3),
        ], start_prev=10.0),
        "000001": _bars([
            ("2026-09-28", 10.0, 10.0),
            ("2026-09-29", 10.0, 10.0),
            ("2026-09-30", 10.0, 10.0),
            ("2026-10-09", 10.0, 10.0),
        ], start_prev=10.0),
    }
    events = {
        "shan": [{"date": "2026-09-28", "holdings": [
            {"code": "600000", "name": "测试股A", "weight": 100, "kind": "ashare_stock", "market": "sh"}
        ], "changes": [], "reason": "建仓"}],
        "fire": [{"date": "2026-09-28", "holdings": [
            {"code": "000001", "name": "测试股B", "weight": 100, "kind": "ashare_stock", "market": "sz"}
        ], "changes": [], "reason": "建仓"}],
    }
    rules = _base_rules("2026-09-28", _two_sleeves())
    result = simulate(rules, events, prices, calendar)
    qsig = [lg for lg in result["logs"] if lg.get("type") == "rebalance" and lg.get("quarter_end")]
    assert qsig and qsig[0]["date"] == "2026-09-30", qsig
    filled = [lg for lg in result["logs"] if lg.get("type") == "rebalance" and lg.get("status") == "filled"]
    assert any(lg["date"] == "2026-10-09" for lg in filled), filled


def test_drawdown_levels():
    calendar = ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"]
    # 四部都持有同一只测试股票，价格从 10 暴跌到 8（-20%），开启降仓后火部、风部目标应下调。
    seq = [
        ("2026-11-09", 10.0, 10.0),
        ("2026-11-10", 10.0, 8.0),
        ("2026-11-11", 8.0, 8.0),
        ("2026-11-12", 8.0, 8.0),
        ("2026-11-13", 8.0, 8.0),
    ]
    prices = {code: _bars(seq, start_prev=10.0) for code in ("600000", "000001", "510300", "000002")}
    events = {
        "shan": [{"date": "2026-11-09", "holdings": [
            {"code": "600000", "name": "A", "weight": 100, "kind": "ashare_stock", "market": "sh"}
        ], "changes": [], "reason": "x"}],
        "fire": [{"date": "2026-11-09", "holdings": [
            {"code": "000001", "name": "B", "weight": 100, "kind": "ashare_stock", "market": "sz"}
        ], "changes": [], "reason": "x"}],
        "feng": [{"date": "2026-11-09", "holdings": [
            {"code": "510300", "name": "C", "weight": 100, "kind": "etf", "market": "sh"}
        ], "changes": [], "reason": "x"}],
        "lin": [{"date": "2026-11-09", "holdings": [
            {"code": "000002", "name": "D", "weight": 100, "kind": "ashare_stock", "market": "sz"}
        ], "changes": [], "reason": "x"}],
    }
    off = _base_rules("2026-11-09", _four_sleeves(), enabled_dd=False)
    disabled = simulate(off, events, prices, calendar)
    risk_off = [lg for lg in disabled["logs"] if lg.get("type") == "risk"]
    assert not risk_off, risk_off
    # 关闭时火部权重应大致留在 30% 附近（齐跌则权重几乎不变）
    w_fire_off = disabled["curves"][-1]["sleeves"]["fire"]["weight"]
    assert abs(w_fire_off - 0.30) < 0.04, w_fire_off

    on = _base_rules("2026-11-09", _four_sleeves(), enabled_dd=True)
    enabled = simulate(on, events, prices, calendar)
    risk_on = [lg for lg in enabled["logs"] if lg.get("type") == "risk"]
    assert any(lg.get("action") == "reduce" and abs(lg.get("level") or 0) >= 0.15 - 1e-9 for lg in risk_on), risk_on
    # 11-10 收盘约 -20%，应触发 20% 档：火部目标 0，风部 10%，次日开盘执行
    last = enabled["curves"][-1]
    assert last["sleeves"]["fire"]["weight"] < 0.08, last["sleeves"]
    assert last["sleeves"]["feng"]["weight"] < 0.16, last["sleeves"]
    assert enabled["hub_cash"] > 0 or last.get("hub_cash", 0) >= 0
    # 总账户现金（hub + 降仓后火部现金）应明显上升
    cashish = last.get("hub_cash", 0) + last["sleeves"]["fire"]["cash"]
    assert cashish > 50_000, (cashish, last)


def test_fund_nav_fill_and_no_stamp():
    calendar = ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12"]
    # 基金无开盘价，open=close=净值
    prices = {
        "006864": _bars([
            ("2026-11-09", 1.5000, 1.5000),
            ("2026-11-10", 1.5100, 1.5100),
            ("2026-11-11", 1.5100, 1.5100),
            ("2026-11-12", 1.5200, 1.5200),
        ], start_prev=1.5000),
        "600000": _bars([
            ("2026-11-09", 10.0, 10.0),
            ("2026-11-10", 10.0, 10.0),
            ("2026-11-11", 10.0, 10.0),
            ("2026-11-12", 10.0, 10.0),
        ], start_prev=10.0),
    }
    events = {
        "lin": [
            {"date": "2026-11-09", "holdings": [
                {"code": "006864", "name": "测试基金", "weight": 100, "kind": "fund", "market": "fund"}
            ], "changes": [], "reason": "建仓"},
            {"date": "2026-11-10", "holdings": [], "changes": ["清仓"], "reason": "清仓"},
        ],
        "shan": [{"date": "2026-11-09", "holdings": [
            {"code": "600000", "name": "测试股A", "weight": 100, "kind": "ashare_stock", "market": "sh"}
        ], "changes": [], "reason": "建仓"}],
    }
    sleeves = {
        "lin": {"id": "lin", "name": "林部", "target": 0.50, "band": 0.05},
        "shan": {"id": "shan", "name": "山部", "target": 0.50, "band": 0.05},
    }
    rules = _base_rules("2026-11-09", sleeves)
    result = simulate(rules, events, prices, calendar)
    buy = next(t for t in result["trades"] if t["code"] == "006864" and t["side"] == "buy")
    sell = next(t for t in result["trades"] if t["code"] == "006864" and t["side"] == "sell")
    assert buy["date"] == "2026-11-10"
    assert abs(buy["open"] - 1.51) < 1e-9
    assert sell["date"] == "2026-11-11", "基金按 T+1 次日可卖"
    assert sell["stamp"] == 0


def test_parse_helpers():
    assert abs(parse_weight("1/8") - 12.5) < 1e-9
    assert abs(parse_weight(22.5) - 22.5) < 1e-9
    ev = parse_feng(
        {"rebalance_date": "2026-11-13", "codes": ["512800", "159928"], "scores": {}},
        {"events": []},
    )
    assert ev and ev[-1]["date"] == "2026-11-13"
    assert abs(sum(h["weight"] for h in ev[-1]["holdings"]) - 100) < 0.01
    lin = parse_lin(
        {"rebalance_date": "2026-11-06", "picks": [
            {"code": "006864", "name": "A", "weight": 50, "style": "成长"},
            {"code": "012060", "name": "B", "weight": 50, "style": "海外"},
        ]},
        {"events": [
            {"date": "2026-11-06", "in": [{"code": "006864", "name": "A", "weight": 100, "style": "成长"}],
             "out": [], "weights": {"成长": 100}, "changes": ["首次"]},
            {"date": "2026-11-09", "in": [{"code": "012060", "name": "B", "weight": 50, "style": "海外"}],
             "out": [], "weights": {"成长": 50, "海外": 50}, "changes": ["补入"]},
        ]},
    )
    assert holdings_asof(lin, "2026-11-06")[0]["code"] == "006864"
    assert {h["code"] for h in holdings_asof(lin, "2026-11-09")} == {"006864", "012060"}
    assert is_quarter_end(["2026-09-30", "2026-10-09"], "2026-09-30")
    assert next_trading_day(["2026-11-09", "2026-11-10"], "2026-11-09") == "2026-11-10"
    assert lot_of("688001", "ashare_stock") == 200
    assert lot_of("006864", "fund") == 0.01



def test_hk_fx_cash_and_mark():
    """港股以港币报价，现金按汇率折人民币；同一价买入并计价，除成本外净值不应因汇率瞬间亏损。"""
    calendar = ["2026-11-09", "2026-11-10", "2026-11-11"]
    prices = {
        "03998": _bars([
            ("2026-11-09", 10.0, 10.0),
            ("2026-11-10", 10.0, 10.0),
            ("2026-11-11", 10.0, 10.0),
        ], start_prev=10.0),
        "600000": _bars([
            ("2026-11-09", 10.0, 10.0),
            ("2026-11-10", 10.0, 10.0),
            ("2026-11-11", 10.0, 10.0),
        ], start_prev=10.0),
    }
    fx = {d: 0.85 for d in calendar}
    events = {
        "shan": [{"date": "2026-11-09", "holdings": [
            {"code": "03998", "name": "测试港股", "weight": 100, "kind": "hk_stock", "market": "hk"}
        ], "changes": [], "reason": "建仓"}],
        "fire": [{"date": "2026-11-09", "holdings": [
            {"code": "600000", "name": "测试股A", "weight": 100, "kind": "ashare_stock", "market": "sh"}
        ], "changes": [], "reason": "建仓"}],
    }
    rules = _base_rules("2026-11-09", _two_sleeves())
    result = simulate(rules, events, prices, calendar, fx_series=fx)
    buy = next(t for t in result["trades"] if t["code"] == "03998")
    assert abs(buy["fx"] - 0.85) < 1e-9
    # 成交额按港币×汇率，约为目标资金而非把港币当成人民币
    assert 400_000 < buy["notional"] < 500_000, buy["notional"]
    row = next(r for r in result["curves"] if r["date"] == "2026-11-10")
    shan_nav = row["sleeves"]["shan"]["nav"]
    # 成本大约是佣金+滑点约 0.13%，不应出现 15% 的汇率错账
    assert shan_nav > 0.99, shan_nav
    assert shan_nav < 1.0, shan_nav
    cash = row["sleeves"]["shan"]["cash"] if "cash" in row["sleeves"]["shan"] else None
    # 总现金不得为负
    assert result["curves"][-1]["sleeves"]["shan"]["equity"] > 0


def test_no_repeat_signal_if_holdings_unchanged():
    calendar = ["2026-11-09", "2026-11-10", "2026-11-11"]
    prices = {"600000": _bars([
        ("2026-11-09", 10.0, 10.0),
        ("2026-11-10", 10.0, 10.0),
        ("2026-11-11", 10.0, 10.0),
    ], start_prev=10.0), "000001": _bars([
        ("2026-11-09", 10.0, 10.0),
        ("2026-11-10", 10.0, 10.0),
        ("2026-11-11", 10.0, 10.0),
    ], start_prev=10.0)}
    same = [{"code": "600000", "name": "A", "weight": 100, "kind": "ashare_stock", "market": "sh"}]
    events = {
        "shan": [
            {"date": "2026-11-09", "holdings": same, "changes": ["建仓"], "reason": "建仓"},
            {"date": "2026-11-10", "holdings": same, "changes": [], "reason": "正式名单"},
        ],
        "fire": [{"date": "2026-11-09", "holdings": [
            {"code": "000001", "name": "B", "weight": 100, "kind": "ashare_stock", "market": "sz"}
        ], "changes": [], "reason": "建仓"}],
    }
    rules = _base_rules("2026-11-09", _two_sleeves())
    result = simulate(rules, events, prices, calendar)
    shan_signals = [lg for lg in result["logs"] if lg.get("sleeve")=="shan" and lg.get("type")=="sleeve_trade"]
    assert len(shan_signals) == 1, shan_signals


CASES = [
    ("未建账不产生净值", test_null_start_date),
    ("A股T+1", test_t_plus_one),
    ("涨跌停顺延", test_limit_defer),
    ("成本扣除", test_costs),
    ("越界再平衡", test_rebalance_band),
    ("季末再平衡", test_quarter_end),
    ("回撤降仓档位", test_drawdown_levels),
    ("基金净值成交且无印花税", test_fund_nav_fill_and_no_stamp),
    ("名单解析与日历辅助", test_parse_helpers),
    ("港股汇率记账", test_hk_fx_cash_and_mark),
    ("名单未变不重复发信号", test_no_repeat_signal_if_holdings_unchanged),
]


def run_all():
    failed = []
    for name, fn in CASES:
        try:
            fn()
            print("  通过  %s" % name)
        except Exception as exc:
            failed.append((name, exc))
            print("  失败  %s: %s" % (name, exc))
    if failed:
        raise SystemExit("自测失败 %d/%d" % (len(failed), len(CASES)))
    print("自测全部通过 %d/%d" % (len(CASES), len(CASES)))
    return True


if __name__ == "__main__":
    run_all()
