"""林部建仓前的公司门槛：不合格或未核实的不得新建仓，对应权重不放大，额度留现金。"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import company_gate

def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def load_from_rules(rules):
    cfg = (rules or {}).get("lin_company_gate") or {}
    if not cfg.get("enabled"):
        return None, None, cfg
    wl_path = cfg.get("whitelist") or "config/company_whitelist.json"
    map_path = cfg.get("company_map") or "config/lin_fund_companies.json"
    if not os.path.isabs(wl_path):
        wl_path = os.path.join(ROOT, wl_path)
    if not os.path.isabs(map_path):
        map_path = os.path.join(ROOT, map_path)
    whitelist = company_gate.load_whitelist(wl_path)
    raw = _load_json(map_path) if os.path.exists(map_path) else {}
    cmap = raw.get("funds") or raw
    return whitelist, cmap, cfg

def company_of(code, cmap):
    rec = (cmap or {}).get(str(code).strip()) or {}
    if isinstance(rec, str):
        return rec
    return rec.get("company")

def filter_holdings(holdings, rules):
    """返回 (kept, dropped)。kept 保留原权重，不重新归一到 100%。"""
    whitelist, cmap, cfg = load_from_rules(rules)
    if not cfg.get("enabled"):
        return list(holdings or []), []
    kept, dropped = [], []
    for h in holdings or []:
        code = str(h.get("code") or "").strip()
        company = company_of(code, cmap)
        chk = company_gate.check(company, whitelist)
        item = dict(h)
        if not chk["pass"]:
            dropped.append({
                "code": code,
                "name": h.get("name"),
                "weight": h.get("weight"),
                "company": company,
                "reason": chk["reason"],
                "rank": chk.get("rank"),
            })
        else:
            item["company"] = chk.get("matched") or company
            item["company_rank"] = chk.get("rank")
            kept.append(item)
    return kept, dropped

def reason_suffix(dropped):
    if not dropped:
        return ""
    parts = []
    for d in dropped:
        parts.append("%s（%s，%s，权重 %s%%）" % (
            d.get("name") or d.get("code"),
            d.get("company") or "公司未核实",
            d.get("reason") or "未通过公司门槛",
            d.get("weight"),
        ))
    cash = sum(float(d.get("weight") or 0) for d in dropped)
    return "公司门槛未纳入：%s。对应林部权重合计 %.1f%% 暂留现金，不放大其余持仓，不强制补入。" % ("；".join(parts), cash)
