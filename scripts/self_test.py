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
from report import classify_band, drawdown_board, limit_status


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



def test_lin_company_gate_blocks_unqualified_and_keeps_cash():
    """不合格或未核实的基金不得进入建仓信号，原权重不放大，额度留现金。"""
    import tempfile
    from pathlib import Path as P
    import lin_company_filter
    calendar = ["2026-11-09", "2026-11-10"]
    prices = {
        "006864": _bars([
            ("2026-11-09", 1.5, 1.5),
            ("2026-11-10", 1.5, 1.5),
        ], start_prev=1.5),
        "012060": _bars([
            ("2026-11-09", 2.0, 2.0),
            ("2026-11-10", 2.0, 2.0),
        ], start_prev=2.0),
        "600000": _bars([
            ("2026-11-09", 10.0, 10.0),
            ("2026-11-10", 10.0, 10.0),
        ], start_prev=10.0),
    }
    events = {
        "lin": [{"date": "2026-11-09", "holdings": [
            {"code": "006864", "name": "不合格基金", "weight": 40, "kind": "fund", "market": "fund"},
            {"code": "012060", "name": "合格基金", "weight": 24.2, "kind": "fund", "market": "fund"},
        ], "changes": [], "reason": "建仓"}],
        "shan": [{"date": "2026-11-09", "holdings": [
            {"code": "600000", "name": "测试股A", "weight": 100, "kind": "ashare_stock", "market": "sh"}
        ], "changes": [], "reason": "建仓"}],
    }
    wl = {
        "top20": [
            {"rank": i, "company": "合格基金管理有限公司" if i == 1 else "其他%02d基金管理有限公司" % i,
             "aliases": ["合格基金"] if i == 1 else [], "mixed_yi": 1000 - i}
            for i in range(1, 21)
        ]
    }
    cmap = {"funds": {
        "006864": {"company": "国联安基金"},
        "012060": {"company": "合格基金"},
    }}
    tmp = P(tempfile.mkdtemp())
    (tmp / "wl.json").write_text(json.dumps(wl, ensure_ascii=False), encoding="utf-8")
    (tmp / "map.json").write_text(json.dumps(cmap, ensure_ascii=False), encoding="utf-8")
    sleeves = {
        "lin": {"id": "lin", "name": "林部", "target": 0.50, "band": 0.05},
        "shan": {"id": "shan", "name": "山部", "target": 0.50, "band": 0.05},
    }
    rules = _base_rules("2026-11-09", sleeves)
    rules["lin_company_gate"] = {
        "enabled": True,
        "whitelist": str(tmp / "wl.json"),
        "company_map": str(tmp / "map.json"),
    }
    result = simulate(rules, events, prices, calendar)
    lin_sig = [lg for lg in result["logs"] if lg.get("sleeve") == "lin" and lg.get("type") == "sleeve_trade"]
    assert len(lin_sig) == 1, lin_sig
    codes = [h["code"] for h in lin_sig[0]["holdings"]]
    assert codes == ["012060"], codes
    assert abs(lin_sig[0]["holdings"][0]["weight"] - 24.2) < 1e-9
    assert any(x.get("code") == "006864" for x in lin_sig[0].get("excluded") or [])
    assert "暂留现金" in (lin_sig[0].get("reason") or "")
    bought = [t["code"] for t in result["trades"] if t.get("side") == "buy"]
    assert "012060" in bought
    assert "006864" not in bought
    # 权重合计不是 100%，不应被放大后买满林部
    buy = next(t for t in result["trades"] if t["code"] == "012060" and t["side"] == "buy")
    # 林部资金 50 万，24.2% 约为 12.1 万，绝不应接近 50 万
    assert buy["notional"] < 200_000, buy["notional"]



def test_market_cache_expires_stale():
    """无时间戳或日期落后于北京时间当天的行情缓存不得复用。"""
    import tempfile, time
    import market
    tmp = tempfile.mkdtemp()
    old_cache = market.CACHE
    market.CACHE = tmp
    try:
        today = market._beijing_today()
        market.cache_put("k", {"bars": {today: {"date": today, "close": 1}}})
        hit = market.cache_get("k")
        assert hit and hit.get("bars")
        # 无时间戳的旧缓存必须丢弃（曾卡在 11-13）
        bare = __import__("pathlib").Path(tmp) / "old"
        bare.write_text(json.dumps({"bars": {"2026-11-13": {"date": "2026-11-13", "close": 1}}}), encoding="utf-8")
        # cache_get uses CACHE/key as filename directly
        stale = market.cache_get("old")
        assert stale is None, stale
        # 有时间戳但行情日期早于北京时间今天，也丢弃
        market.cache_put("oldbar", {"bars": {"2026-11-13": {"date": "2026-11-13", "close": 1}}})
        assert market.cache_get("oldbar") is None
    finally:
        market.CACHE = old_cache


def _fee_cents(value):
    return int(round(float(value or 0) * 100))


def _assert_fees_match(result):
    fees = result["ledger"]["fees"]
    filled = result["ledger"]["filled"]
    assert _fee_cents(fees["commission"]) == sum(_fee_cents(t["commission"]) for t in filled)
    assert _fee_cents(fees["stamp"]) == sum(_fee_cents(t["stamp"]) for t in filled)
    assert _fee_cents(fees["slippage"]) == sum(_fee_cents(t["slippage"]) for t in filled)
    assert _fee_cents(fees["total"]) == (
        _fee_cents(fees["commission"]) + _fee_cents(fees["stamp"]) + _fee_cents(fees["slippage"])
    )
    assert filled and all(t["status"] == "filled" for t in filled)
    for t in filled:
        part = _fee_cents(t["commission"]) + _fee_cents(t["stamp"]) + _fee_cents(t["slippage"])
        assert _fee_cents(t["fee_total"]) == part


def _trade_sig(result):
    return [
        (t["date"], t["code"], t["side"], t["shares"], t["notional"], t["commission"], t["stamp"])
        for t in result["trades"]
    ]


def test_band_and_limit_status_math():
    inside = classify_band(0.30, 0.30, 0.05)
    assert inside["status"] == "区间内" and inside["inside"] is True and inside["action"] == "仅展示"
    above = classify_band(0.36, 0.30, 0.05)
    assert above["status"] == "高于上限" and above["inside"] is False
    below = classify_band(0.24, 0.30, 0.05)
    assert below["status"] == "低于下限"
    edge = classify_band(0.349, 0.30, 0.05)
    assert edge["status"] == "接近边界" and edge["inside"] is True
    missing = classify_band(None, 0.30, 0.05)
    assert missing["status"] == "无权重" and missing["weight"] is None
    assert limit_status(None, 0.08, missing=True) == "缺价"
    assert limit_status(0.05, 0.08) == "未接近"
    assert limit_status(0.064, 0.08) == "接近"
    assert limit_status(0.08, 0.08) == "超过"
    levels = [
        {"drawdown": 0.10, "action": "warn", "note": "预警"},
        {"drawdown": 0.15, "action": "reduce", "targets": {"fire": 0.15, "feng": 0.15}, "note": "一档"},
        {"drawdown": 0.20, "action": "reduce", "targets": {"fire": 0.0, "feng": 0.10}, "note": "二档"},
    ]
    board = drawdown_board(-0.12, levels, enabled=True)
    assert abs(board["magnitude"] - 0.12) < 1e-12
    by = {row["drawdown"]: row for row in board["levels"]}
    assert by[0.10]["reached"] is True and abs(by[0.10]["distance"] - (0.10 - 0.12)) < 1e-12
    assert by[0.15]["reached"] is False and abs(by[0.15]["distance"] - 0.03) < 1e-12
    assert by[0.20]["reached"] is False
    assert board["active"]["action"] == "warn"
    assert board["levels"][2]["targets"]["fire"] == 0.0
    assert board["display_only"] is True
    quiet = drawdown_board(None, levels, enabled=True)
    assert quiet["magnitude"] is None and all(row["distance"] is None and row["reached"] is False for row in quiet["levels"])


def test_ledger_pending_differs_from_filled():
    """涨停顺延停在待成交；已成交的买入不进待成交。卖出信号未到开盘，也不写成已成交。"""
    calendar = ["2026-11-09", "2026-11-10"]
    prices = {
        "600000": _bars([
            ("2026-11-09", 10.00, 10.00),
            ("2026-11-10", 11.00, 11.00),
        ], start_prev=10.00),
        "000001": _bars([
            ("2026-11-09", 10.00, 10.00),
            ("2026-11-10", 10.00, 11.00),
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
    ledger = result["ledger"]
    assert not any(t["code"] == "600000" for t in ledger["filled"])
    deferred = [p for p in ledger["pending"] if p.get("code") == "600000"]
    assert len(deferred) == 1 and deferred[0]["status"] == "deferred"
    assert "涨停" in deferred[0]["reason"] and deferred[0]["side"] == "buy"
    assert deferred[0]["current_shares"] == 0
    assert ("000001", "buy") in {(t["code"], t["side"]) for t in ledger["filled"]}
    assert ("000001", "sell") not in {(t["code"], t["side"]) for t in ledger["filled"]}
    sell_wait = [p for p in ledger["pending"] if p.get("code") == "000001"]
    assert len(sell_wait) == 1 and sell_wait[0]["status"] == "signal" and sell_wait[0]["side"] == "sell"
    assert "不估算" in sell_wait[0]["reason"]
    assert all(t.get("code") != "600000" for t in result["trades"])
    held = {p["code"] for p in ledger["positions"]}
    assert held == {"000001"}
    _assert_fees_match(result)

    done_cal = calendar + ["2026-11-11", "2026-11-12"]
    prices["600000"] = _bars([
        ("2026-11-09", 10.00, 10.00),
        ("2026-11-10", 11.00, 11.00),
        ("2026-11-11", 10.50, 10.50),
        ("2026-11-12", 10.50, 10.50),
    ], start_prev=10.00)
    prices["000001"] = _bars([
        ("2026-11-09", 10.00, 10.00),
        ("2026-11-10", 10.00, 11.00),
        ("2026-11-11", 9.90, 9.90),
        ("2026-11-12", 10.00, 10.00),
    ], start_prev=10.00)
    done = simulate(rules, events, prices, done_cal)
    assert any(t["code"] == "600000" and t["side"] == "buy" and t["status"] == "filled" for t in done["ledger"]["filled"])
    assert any(t["code"] == "000001" and t["side"] == "sell" and t["status"] == "filled" for t in done["ledger"]["filled"])
    assert not any(p.get("code") in ("600000", "000001") for p in done["ledger"]["pending"])
    _assert_fees_match(done)


def test_ledger_fees_match_lines_and_hk_stamp_gap():
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
    result = simulate(_base_rules("2026-11-09", _two_sleeves()), events, prices, calendar)
    _assert_fees_match(result)
    stock_sell = next(t for t in result["ledger"]["filled"] if t["code"] == "600000" and t["side"] == "sell")
    etf_sell = next(t for t in result["ledger"]["filled"] if t["code"] == "510300" and t["side"] == "sell")
    assert stock_sell["stamp"] > 0 and etf_sell["stamp"] == 0
    assert _fee_cents(result["ledger"]["fees"]["stamp"]) == _fee_cents(stock_sell["stamp"])
    assert result["ledger"]["positions"] == []

    hk_prices = {
        "03998": _bars([
            ("2026-11-09", 10.0, 10.0),
            ("2026-11-10", 10.0, 10.0),
        ], start_prev=10.0),
        "600000": _bars([
            ("2026-11-09", 10.0, 10.0),
            ("2026-11-10", 10.0, 10.0),
        ], start_prev=10.0),
    }
    hk_events = {
        "shan": [{"date": "2026-11-09", "holdings": [
            {"code": "03998", "name": "测试港股", "weight": 100, "kind": "hk_stock", "market": "hk"}
        ], "changes": [], "reason": "建仓"}],
        "fire": [{"date": "2026-11-09", "holdings": [
            {"code": "600000", "name": "测试股A", "weight": 100, "kind": "ashare_stock", "market": "sh"}
        ], "changes": [], "reason": "建仓"}],
    }
    hk = simulate(
        _base_rules("2026-11-09", _two_sleeves()), hk_events, hk_prices,
        ["2026-11-09", "2026-11-10"], fx_series={"2026-11-09": 0.85, "2026-11-10": 0.85},
    )
    hk_buy = next(t for t in hk["ledger"]["filled"] if t["code"] == "03998")
    assert hk_buy["stamp"] == 0
    _assert_fees_match(hk)
    assert any("港股印花税未单列" in g["reason"] for g in hk["ledger"]["gaps"])
    assert not any(t["stamp"] > 0 and t["kind"] == "hk_stock" for t in hk["ledger"]["filled"])


def test_t_plus_marks():
    calendar = ["2026-11-09", "2026-11-10"]
    prices = {
        "600000": _bars([
            ("2026-11-09", 10.0, 10.0),
            ("2026-11-10", 10.0, 10.0),
        ], start_prev=10.0),
        "03998": _bars([
            ("2026-11-09", 10.0, 10.0),
            ("2026-11-10", 10.0, 10.0),
        ], start_prev=10.0),
    }
    events = {
        "shan": [{"date": "2026-11-09", "holdings": [
            {"code": "600000", "name": "测试股A", "weight": 100, "kind": "ashare_stock", "market": "sh"}
        ], "changes": [], "reason": "建仓"}],
        "fire": [{"date": "2026-11-09", "holdings": [
            {"code": "03998", "name": "测试港股", "weight": 100, "kind": "hk_stock", "market": "hk"}
        ], "changes": [], "reason": "建仓"}],
    }
    rules = _base_rules("2026-11-09", _two_sleeves())
    fx = {d: 0.85 for d in calendar}
    locked = simulate(rules, events, prices, calendar, fx_series=fx)
    a = next(row for row in locked["ledger"]["locks"] if row["code"] == "600000")
    hk = next(row for row in locked["ledger"]["locks"] if row["code"] == "03998")
    assert a["status"] == "T+1锁定"
    assert a["locked_shares"] == a["shares"] and a["sellable_shares"] == 0
    assert a["lots"][0]["sellable_on"] is None
    assert hk["status"] == "T+0 当日可卖" and hk["locked_shares"] == 0 and hk["sellable_shares"] == hk["shares"]

    later_cal = calendar + ["2026-11-11"]
    prices["600000"]["2026-11-11"] = {
        "date": "2026-11-11", "open": 10.0, "close": 10.0, "high": 10.0, "low": 10.0, "prev": 10.0,
    }
    prices["03998"]["2026-11-11"] = {
        "date": "2026-11-11", "open": 10.0, "close": 10.0, "high": 10.0, "low": 10.0, "prev": 10.0,
    }
    opened = simulate(rules, events, prices, later_cal, fx_series={d: 0.85 for d in later_cal})
    a2 = next(row for row in opened["ledger"]["locks"] if row["code"] == "600000")
    assert a2["status"] == "可卖" and a2["locked_shares"] == 0 and a2["sellable_shares"] == a2["shares"]
    assert a2["lots"][0]["sellable_on"] == "2026-11-11"

    fund_prices = {
        "006864": _bars([
            ("2026-11-09", 1.5, 1.5),
            ("2026-11-10", 1.51, 1.51),
        ], start_prev=1.5),
        "600000": prices["600000"],
    }
    fund_events = {
        "lin": [{"date": "2026-11-09", "holdings": [
            {"code": "006864", "name": "测试基金", "weight": 100, "kind": "fund", "market": "fund"}
        ], "changes": [], "reason": "建仓"}],
        "shan": events["shan"],
    }
    sleeves = {
        "lin": {"id": "lin", "name": "林部", "target": 0.50, "band": 0.05},
        "shan": {"id": "shan", "name": "山部", "target": 0.50, "band": 0.05},
    }
    fund = simulate(_base_rules("2026-11-09", sleeves), fund_events, fund_prices, calendar)
    fund_lock = next(row for row in fund["ledger"]["locks"] if row["code"] == "006864")
    assert fund_lock["status"] == "T+1锁定" and fund_lock["sellable_shares"] == 0


def test_r1_distance_on_book():
    calendar = ["2026-11-09", "2026-11-10", "2026-11-11"]
    flat = [
        ("2026-11-09", 10.0, 10.0),
        ("2026-11-10", 10.0, 10.0),
        ("2026-11-11", 10.0, 10.0),
    ]
    prices = {code: _bars(flat, start_prev=10.0) for code in ("600000", "000001", "510300", "000002")}
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
    rules = _base_rules("2026-11-09", _four_sleeves(), enabled_dd=True)
    quiet = simulate(rules, events, prices, calendar)
    board = quiet["risk"]["drawdown"]
    mag = board["magnitude"]
    assert mag is not None and mag < 0.10
    for row in board["levels"]:
        assert row["reached"] is False
        assert abs(row["distance"] - (row["drawdown"] - mag)) < 1e-9
    assert board["active"] is None and board["enabled"] is True and board["display_only"] is True

    dropped = [
        ("2026-11-09", 10.0, 10.0),
        ("2026-11-10", 10.0, 8.0),
        ("2026-11-11", 8.0, 8.0),
        ("2026-11-12", 8.0, 8.0),
    ]
    deep_prices = {code: _bars(dropped, start_prev=10.0) for code in prices}
    deep = simulate(rules, events, deep_prices, calendar + ["2026-11-12"])
    deep_board = deep["risk"]["drawdown"]
    deep_mag = deep_board["magnitude"]
    assert deep_mag >= 0.15
    for row in deep_board["levels"]:
        assert abs(row["distance"] - (row["drawdown"] - deep_mag)) < 1e-9
        assert row["reached"] == (deep_mag + 1e-12 >= row["drawdown"])
    assert deep_board["applied_targets"] == {k: float(v) for k, v in deep["capital_targets"].items()}
    assert deep["risk"]["display_only"] is True
    # 测试夹具的 20% 档把火部写成 0，仪表必须照抄规则，不能换成另一套档位
    level20 = next(row for row in deep_board["levels"] if abs(row["drawdown"] - 0.20) < 1e-12)
    assert level20["targets"]["fire"] == 0.0


def test_single_name_and_industry_report_only():
    calendar = ["2026-11-09", "2026-11-10", "2026-11-11"]
    seq = [
        ("2026-11-09", 10.0, 10.0),
        ("2026-11-10", 10.0, 10.0),
        ("2026-11-11", 10.0, 10.0),
    ]
    prices = {
        "600000": _bars(seq, start_prev=10.0),
        "000001": _bars(seq, start_prev=10.0),
    }
    bare = {
        "shan": [{"date": "2026-11-09", "holdings": [
            {"code": "600000", "name": "山", "weight": 100, "kind": "ashare_stock", "market": "sh"}
        ], "changes": [], "reason": "建仓"}],
        "fire": [{"date": "2026-11-09", "holdings": [
            {"code": "000001", "name": "火", "weight": 100, "kind": "ashare_stock", "market": "sz"}
        ], "changes": [], "reason": "建仓"}],
    }
    tagged = {
        "shan": [{"date": "2026-11-09", "holdings": [
            {"code": "600000", "name": "山", "weight": 100, "kind": "ashare_stock", "market": "sh", "industry": "银行"}
        ], "changes": [], "reason": "建仓"}],
        "fire": [{"date": "2026-11-09", "holdings": [
            {"code": "000001", "name": "火", "weight": 100, "kind": "ashare_stock", "market": "sz", "industry": "银行"}
        ], "changes": [], "reason": "建仓"}],
    }
    rules = _base_rules("2026-11-09", _two_sleeves())
    rules["position_limits"] = {
        "single_stock_max_of_total": 0.08,
        "industry_lookthrough_max_of_total": 0.30,
    }
    plain = simulate(rules, bare, prices, calendar)
    marked = simulate(rules, tagged, prices, calendar)
    assert _trade_sig(plain) == _trade_sig(marked)
    assert marked["capital_targets"]["fire"] == plain["capital_targets"]["fire"] == 0.5
    row = next(r for r in marked["risk"]["single_stock"]["rows"] if r["code"] == "000001")
    assert row["status"] == "超过" and row["weight_of_total"] > 0.08
    assert row["action"] == "仅展示，不调仓"
    ind = next(r for r in marked["risk"]["industry"]["rows"] if r["industry"] == "银行")
    assert ind["status"] == "超过" and ind["weight_of_total"] > 0.30
    assert {p["code"] for p in ind["positions"]} == {"600000", "000001"}
    assert ind["action"] == "仅报告，不强制调仓"
    assert all("单票" not in (t.get("note") or "") and "行业" not in (t.get("note") or "") for t in marked["trades"])
    assert marked["risk"]["display_only"] is True

    # 没标签的大仓不能算进行业，也不能并进已有行业
    near_sleeves = {
        "shan": {"id": "shan", "name": "山部", "target": 0.93, "band": 0.05},
        "fire": {"id": "fire", "name": "火部", "target": 0.07, "band": 0.05},
    }
    near_events = {
        "shan": bare["shan"],
        "fire": [{"date": "2026-11-09", "holdings": [
            {"code": "000001", "name": "火", "weight": 100, "kind": "ashare_stock", "market": "sz", "industry": "银行"}
        ], "changes": [], "reason": "建仓"}],
    }
    near = simulate(_base_rules("2026-11-09", near_sleeves), near_events, prices, calendar)
    fire_row = next(r for r in near["risk"]["single_stock"]["rows"] if r["code"] == "000001")
    assert fire_row["status"] == "接近" and fire_row["weight_of_total"] < 0.08
    assert all(r["status"] != "超过" for r in near["risk"]["industry"]["rows"])
    assert any(u["code"] == "600000" and u["reason"] == "行业分类缺失" for u in near["risk"]["industry"]["unclassified"])
    bank = next(r for r in near["risk"]["industry"]["rows"] if r["industry"] == "银行")
    assert {p["code"] for p in bank["positions"]} == {"000001"}
    assert any("缺少行业分类" in g["reason"] for g in near["risk"]["gaps"])


def test_band_breach_is_display_only():
    calendar = ["2026-11-09", "2026-11-10", "2026-11-11"]
    prices = {
        "600000": _bars([
            ("2026-11-09", 10.0, 10.0),
            ("2026-11-10", 10.0, 13.0),
            ("2026-11-11", 13.0, 13.0),
        ], start_prev=10.0),
        "000001": _bars([
            ("2026-11-09", 10.0, 10.0),
            ("2026-11-10", 10.0, 10.0),
            ("2026-11-11", 10.0, 10.0),
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
    rules["rebalance"] = {"quarter_end_check": False, "band_trigger": False, "pull_to": "target"}
    result = simulate(rules, events, prices, calendar)
    shan = next(b for b in result["risk"]["bands"] if b["id"] == "shan")
    fire = next(b for b in result["risk"]["bands"] if b["id"] == "fire")
    assert shan["status"] == "高于上限" and shan["inside"] is False and shan["action"] == "仅展示"
    assert fire["status"] == "低于下限" and fire["action"] == "仅展示"
    assert not any(lg.get("type") == "rebalance" for lg in result["logs"])
    assert result["capital_targets"]["shan"] == 0.5
    assert not any(p.get("status") == "macro" for p in result["ledger"]["pending"])

    live = _base_rules("2026-11-09", _two_sleeves())
    stopped = simulate(live, events, prices, ["2026-11-09", "2026-11-10"])
    assert any(p.get("status") == "macro" for p in stopped["ledger"]["pending"])
    assert not any(lg.get("type") == "rebalance" and lg.get("status") == "filled" for lg in stopped["logs"])
    shan_now = next(b for b in stopped["risk"]["bands"] if b["id"] == "shan")
    assert shan_now["status"] == "高于上限"
    assert all(t["side"] == "buy" for t in stopped["trades"])


def test_ledger_does_not_invent_positions():
    empty = not_started(_base_rules(None, _four_sleeves()))
    assert empty["nav"] is None and empty["ledger"]["positions"] == []
    assert empty["ledger"]["filled"] == [] and empty["ledger"]["pending"] == []
    assert empty["risk"]["drawdown"]["magnitude"] is None
    assert empty["risk"]["single_stock"]["rows"] == []
    assert all(b["status"] == "无权重" for b in empty["risk"]["bands"])

    calendar = ["2026-11-09"]
    prices = {"600000": _bars([("2026-11-09", 10.0, 10.0)], start_prev=10.0)}
    events = {"shan": [{"date": "2026-11-09", "holdings": [
        {"code": "600000", "name": "测试股A", "weight": 100, "kind": "ashare_stock", "market": "sh"}
    ], "changes": ["建仓"], "reason": "建仓"}]}
    sleeves = {"shan": {"id": "shan", "name": "山部", "target": 1.0, "band": 0.05}}
    signal_only = simulate(_base_rules("2026-11-09", sleeves), events, prices, calendar)
    assert signal_only["trades"] == []
    assert signal_only["ledger"]["positions"] == []
    assert signal_only["ledger"]["filled"] == []
    assert any(p["code"] == "600000" and p["status"] == "signal" for p in signal_only["ledger"]["pending"])

    # 更新的空名单覆盖更早的持仓，旧代码不能留在当前仓位里
    prices2 = {
        "600000": _bars([
            ("2026-11-09", 10.0, 10.0),
            ("2026-11-10", 10.0, 10.0),
        ], start_prev=10.0),
        "000001": _bars([
            ("2026-11-09", 10.0, 10.0),
            ("2026-11-10", 10.0, 10.0),
        ], start_prev=10.0),
    }
    old_events = {
        "fire": [
            {"date": "2026-11-01", "holdings": [
                {"code": "000001", "name": "旧仓", "weight": 100, "kind": "ashare_stock", "market": "sz"}
            ], "changes": [], "reason": "旧"},
            {"date": "2026-11-09", "holdings": [], "changes": ["空仓"], "reason": "空仓"},
        ],
        "shan": [{"date": "2026-11-09", "holdings": [
            {"code": "600000", "name": "测试股A", "weight": 100, "kind": "ashare_stock", "market": "sh"}
        ], "changes": [], "reason": "建仓"}],
    }
    cleared = simulate(_base_rules("2026-11-09", _two_sleeves()), old_events, prices2, ["2026-11-09", "2026-11-10"])
    assert all(p["code"] != "000001" for p in cleared["ledger"]["positions"])
    assert all(t["code"] != "000001" for t in cleared["trades"])
    assert all(p.get("code") != "000001" for p in cleared["ledger"]["pending"])
    assert any(s["id"] == "fire" for s in cleared["ledger"]["empty_sleeves"])

    # 有仓但缺截止日价：不把旧收盘填成占比，也不写成 0% 未超限
    gap_prices = {
        "000001": _bars([
            ("2026-11-09", 10.0, 10.0),
            ("2026-11-10", 10.0, 10.0),
        ], start_prev=10.0),
    }
    gap_events = {"fire": [{"date": "2026-11-09", "holdings": [
        {"code": "000001", "name": "火", "weight": 100, "kind": "ashare_stock", "market": "sz", "industry": "银行"}
    ], "changes": [], "reason": "建仓"}]}
    gap_rules = _base_rules("2026-11-09", {"fire": {"id": "fire", "name": "火部", "target": 1.0, "band": 0.05}})
    gap = simulate(gap_rules, gap_events, gap_prices, ["2026-11-09", "2026-11-10", "2026-11-11"])
    pos = next(p for p in gap["ledger"]["positions"] if p["code"] == "000001")
    assert pos["shares"] > 0 and pos["price_gap"] is True and pos["market_value"] is None and pos["close"] is None
    row = gap["risk"]["single_stock"]["rows"][0]
    assert row["status"] == "缺价" and row["weight_of_total"] is None
    assert gap["risk"]["industry"]["rows"] == []
    assert any(x["code"] == "000001" for x in gap["risk"]["industry"]["incomplete"])
    assert any("不沿用旧价" in g["reason"] for g in gap["risk"]["gaps"])


def test_industry_label_passthrough_does_not_guess():
    from boards import parse_feng, parse_fire, parse_lin, parse_shan
    shan = parse_shan(
        {"rebalance_date": "2026-11-20", "items": [
            {"code": "601528", "name": "瑞丰银行", "weight": 10, "ind": "银行"},
            {"code": "300750", "name": "宁德时代", "weight": 22.5},
        ]},
        {"events": []},
    )
    by = {h["code"]: h for h in shan[-1]["holdings"]}
    assert by["601528"]["industry"] == "银行" and by["601528"]["industry_source"] == "board_ind"
    assert not by["300750"].get("industry")
    feng = parse_feng(
        {"rebalance_date": "2026-11-20", "codes": ["512800", "510300"]},
        {"events": []},
        {"rows": [
            {"code": "512800", "name": "银行ETF", "sector": "银行"},
            {"code": "510300", "name": "沪深300ETF"},
        ]},
    )
    by = {h["code"]: h for h in feng[-1]["holdings"]}
    assert by["512800"]["industry"] == "银行" and by["512800"]["industry_source"] == "board_sector"
    assert not by["510300"].get("industry")
    fire = parse_fire(
        {"rebalance_date": "2026-11-25", "items": [
            {"code": "000001", "name": "某股", "weight": 100, "theme": "机器人"},
        ]},
        {"events": []},
    )
    assert not fire[-1]["holdings"][0].get("industry")
    lin = parse_lin(
        {"rebalance_date": "2026-11-06", "picks": [
            {"code": "012060", "name": "富国", "weight": 24.2, "style": "海外"},
        ]},
        {"events": []},
    )
    assert not lin[-1]["holdings"][0].get("industry")
    blank = parse_shan(
        {"rebalance_date": "2026-11-20", "items": [
            {"code": "601528", "name": "瑞丰银行", "weight": 100, "ind": "其他"},
        ]},
        {"events": []},
    )
    assert not blank[-1]["holdings"][0].get("industry")


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
    ("林部公司门槛拦截不合格并不放大仓位", test_lin_company_gate_blocks_unqualified_and_keeps_cash),
    ("行情缓存过期不再复用旧K线", test_market_cache_expires_stale),
    ("区间与限额状态", test_band_and_limit_status_math),
    ("待成交与已成交区分", test_ledger_pending_differs_from_filled),
    ("费用合计与明细一致且港股印花税不编造", test_ledger_fees_match_lines_and_hk_stamp_gap),
    ("T+1锁定标记", test_t_plus_marks),
    ("R1距离", test_r1_distance_on_book),
    ("单票8%与行业30%只报告", test_single_name_and_industry_report_only),
    ("区间破坏只展示不调仓", test_band_breach_is_display_only),
    ("空仓与缺数据不编造持仓", test_ledger_does_not_invent_positions),
    ("行业标签只搬运不猜测", test_industry_label_passthrough_does_not_guess),
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
