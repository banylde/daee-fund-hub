#!/usr/bin/env python3
"""把大鹅基金总览导出为自包含离线 HTML。不发布、不上传、不开服务。"""
import argparse, json, base64, datetime as dt
from pathlib import Path

BJ = dt.timezone(dt.timedelta(hours=8))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hub", default="/workspace/daee-fund-hub")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    hub = Path(args.hub)
    html = (hub / "index.html").read_text(encoding="utf-8")
    summary = (hub / "data" / "summary.json").read_text(encoding="utf-8")
    js = (hub / "vendor" / "lightweight-charts" / "lightweight-charts.standalone.production.js").read_text(encoding="utf-8")
    icon = (hub / "icons" / "icon.svg").read_bytes()
    icon_uri = "data:image/svg+xml;base64," + base64.b64encode(icon).decode("ascii")
    if "</script>" in js.lower():
        js = js.replace("</script>", "<\\/script>").replace("</SCRIPT>", "<\\/SCRIPT>")
    data_js = "const __OFFLINE_SUMMARY__ = " + summary + ";\n"
    if "</script>" in data_js.lower():
        raise SystemExit("summary.json 含脚本结束标签，拒绝内嵌")

    exported_at = dt.datetime.now(BJ).strftime("%Y-%m-%d %H:%M")
    meta = json.loads(summary)
    banner = (
        '<div class="disclaimer" role="note" style="border-color:#e5484d">'
        "<b>离线快照，不是持续更新页面。</b>"
        f" 导出时间 {exported_at}（北京时间）。"
        f" 快照内数据：建账日 {meta.get('start_date') or '—'}，"
        f"数据日期 {meta.get('asof') or '尚未产生'}，"
        f"页面更新于 {meta.get('updated_at') or '—'}（{meta.get('timezone') or 'Asia/Shanghai'}）。"
        " 请用浏览器直接打开本文件，无需联网、无需本地服务器。"
        " 模拟盘，非投资建议。请勿公开发布本文件或其中的账户持仓。"
        "</div>"
    )

    html = html.replace('<link rel="manifest" href="manifest.webmanifest">', "")
    html = html.replace('href="icons/icon.svg"', f'href="{icon_uri}"')
    html = html.replace("<title>大鹅基金模拟盘</title>", "<title>大鹅基金模拟盘（离线快照）</title>")
    html = html.replace(
        '<script src="vendor/lightweight-charts/lightweight-charts.standalone.production.js" onerror="window.__lwChartsFailed=1"></script>',
        "<script>" + js + "</script>",
    )
    html = html.replace(
        'fetch("data/summary.json",{cache:"no-store"}).then(r=>{\n'
        '  if(!r.ok) throw new Error("summary "+r.status);\n'
        "  return r.json();\n"
        "}).then(render).catch(err=>{\n",
        "Promise.resolve(__OFFLINE_SUMMARY__).then(render).catch(err=>{\n",
    )
    # insert data script and banner
    html = html.replace("<script>\nconst ACC=", "<script>\n" + data_js + "const ACC=", 1)
    html = html.replace('<div class="disclaimer" role="note">', banner + '\n<div class="disclaimer" role="note">', 1)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print("wrote", out, "bytes", out.stat().st_size)
    print("start_date", meta.get("start_date"), "asof", meta.get("asof"), "updated_at", meta.get("updated_at"), "started", meta.get("started"))

if __name__ == "__main__":
    main()
