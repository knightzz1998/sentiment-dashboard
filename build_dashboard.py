# -*- coding: utf-8 -*-
"""A 股情绪看板 · 数据构建与页面生成

用法：
  python build_dashboard.py --init     # 首次：从本机通达信缓存生成种子历史 + 代码表
  python build_dashboard.py            # 日常：拉最新行情 -> 算情绪 -> 生成 index.html
  python build_dashboard.py --offline  # 只用已有数据重绘页面（不联网）

数据来源：新浪财经行情接口（日线快照）＋ 本机通达信前复权日线（仅初始化用）
口径：情绪值 = 50%×涨停家数分位 + 30%×连板家数分位 + 20%×(100−炸板率分位)，分位窗口 60 个交易日
输出：data/sentiment.json、data/codes.json、index.html
"""
import argparse
import json
import re
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
HIST = DATA / "sentiment.json"
CODES = DATA / "codes.json"
CACHE = Path(r"D:\Code\GS\ETF\data\sbl_qfq_bars_cache.json")   # 仅 --init 用
WARM = 60
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


# ─────────────────────────── 工具 ───────────────────────────
def limit_of(code, name=""):
    """该股涨停阈值（%）"""
    if "ST" in name.upper():
        return 4.8
    if code[:3] in ("688", "689", "300", "301"):
        return 19.6
    if code[:1] in ("4", "8") or code[:2] == "92":
        return 29.6
    return 9.6


def prank(seq, n=WARM):
    """seq[-1] 在最近 n 个值中的分位 0~100"""
    win = seq[-n:]
    if len(win) < 10:
        return None
    x = win[-1]
    less = sum(1 for v in win if v < x)
    eq = sum(1 for v in win if v == x)
    return (less + 0.5 * eq) / len(win) * 100.0


# ─────────────────────── 新浪行情抓取 ───────────────────────
def sina_symbol(code):
    if code[:1] == "6" or code[:3] in ("688", "689"):
        return "sh" + code
    if code[:1] in ("4", "8") or code[:2] == "92":
        return "bj" + code
    return "sz" + code


def fetch_sina(codes, batch=700, pause=0.25):
    """返回 {code: dict(name, open, prev, now, high, low, vol)}"""
    out = {}
    total = (len(codes) + batch - 1) // batch
    for i in range(0, len(codes), batch):
        part = codes[i:i + batch]
        url = "http://hq.sinajs.cn/list=" + ",".join(sina_symbol(c) for c in part)
        req = urllib.request.Request(url, headers={"Referer": "https://finance.sina.com.cn",
                                                  "User-Agent": UA})
        for attempt in range(3):
            try:
                raw = urllib.request.urlopen(req, timeout=25).read().decode("gbk", "ignore")
                break
            except Exception as e:
                if attempt == 2:
                    print("  批次 %d/%d 失败：%s" % (i // batch + 1, total, e))
                    raw = ""
                time.sleep(1.5)
        for m in re.finditer(r'hq_str_([a-z]{2})(\d{6})="([^"]*)"', raw):
            code, body = m.group(2), m.group(3)
            f = body.split(",")
            if len(f) < 10:
                continue
            try:
                prev, now = float(f[2]), float(f[3])
                if prev <= 0 or now <= 0:
                    continue
                out[code] = {"name": f[0], "open": float(f[1]), "prev": prev,
                             "now": now, "high": float(f[4]), "low": float(f[5]),
                             "vol": float(f[8]), "date": f[30] if len(f) > 30 else ""}
            except (ValueError, IndexError):
                continue
        time.sleep(pause)
    return out


# ─────────────────────── 单日指标计算 ───────────────────────
def day_stats(quotes, prev_lu):
    """quotes: {code: {...}}；prev_lu: 上一交易日的涨停代码集合"""
    up = dn = touch = sealed = 0
    ups = downs = 0
    lb2 = 0
    lbh = 0
    lu = []
    chgs = []
    for code, q in quotes.items():
        lim = limit_of(code, q["name"])
        chg = (q["now"] / q["prev"] - 1) * 100
        hchg = (q["high"] / q["prev"] - 1) * 100
        chgs.append(chg)
        if chg > 0:
            ups += 1
        elif chg < 0:
            downs += 1
        is_lu = chg >= lim
        if is_lu:
            up += 1
            lu.append(code)
        elif chg <= -lim:
            dn += 1
        if hchg >= lim:
            touch += 1
            if is_lu:
                sealed += 1
    # 连板：今日涨停 且 昨日也涨停
    lb_codes = [c for c in lu if c in prev_lu]
    lb2 = len(lb_codes)
    # 最高连板高度：用历史累计（简化：连续在榜天数）
    return {"up": up, "dn": dn, "touch": touch, "sealed": sealed,
            "ups": ups, "downs": downs, "lb2": lb2, "lu": lu,
            "eq": round(sum(chgs) / len(chgs), 4) if chgs else 0.0}


def chain_height(code, daily, maxdays=12):
    """回溯该股连续涨停天数（用历史 lu 名单）"""
    n = 1
    for rec in reversed(daily[-maxdays:]):
        if code in set(rec.get("lu", [])):
            n += 1
        else:
            break
    return n


# ─────────────────────── 情绪值与阶段 ───────────────────────
def emo_series(daily):
    ups_ = [d["up"] for d in daily]
    lbs_ = [d["lb2"] for d in daily]
    brs_ = [d["br"] for d in daily]
    out = []
    for i in range(len(daily)):
        if i + 1 < 10:
            out.append(None)
            continue
        r1 = prank(ups_[:i + 1])
        r2 = prank(lbs_[:i + 1])
        r3 = prank(brs_[:i + 1])
        if r1 is None or r2 is None or r3 is None:
            out.append(None)
        else:
            out.append(0.5 * r1 + 0.3 * r2 + 0.2 * (100 - r3))
    return out


def phase_of(emos, i):
    e = emos[i]
    if e is None:
        return "数据不足"
    w = [x for x in emos[max(0, i - 9):i + 1] if x is not None]
    if len(w) < 8:
        return "数据不足"
    ma5 = sum(w[-5:]) / 5.0
    ma5p = sum(w[-6:-1]) / 5.0 if len(w) >= 6 else ma5
    rise = ma5 > ma5p
    if e < 25:
        return "冰点"
    if e >= 75 and rise:
        return "高潮"
    if e >= 55 and rise:
        return "发酵"
    if e < 40:
        return "启动" if rise else "退潮"
    return "震荡"


PHASE_ADVICE = {
    "冰点": ("只低吸或空仓，不碰打板", "情绪值 <25。分批买宽基指数可以开始，打板一律停手。"),
    "启动": ("小仓位试错，只做最强的", "情绪从低位回升，有资金试盘。只参与最强的那个方向。"),
    "发酵": ("可以主动进攻", "赚钱效应扩散，这是短线唯一可以进攻的窗口。"),
    "高潮": ("只减不加", "情绪过热，溢价最贵。不加仓，考虑分批减。"),
    "退潮": ("停手，不接飞刀", "情绪转冷、炸板增多。这段时间打板的历史中位数最差。"),
    "震荡": ("降低频率，等信号", "没有方向，来回切换。不做比乱做好。"),
    "数据不足": ("—", "历史样本不足 60 个交易日，等数据积累。"),
}


# ─────────────────────── 初始化 ───────────────────────
def do_init():
    if not CACHE.exists():
        raise SystemExit("找不到通达信缓存：%s" % CACHE)
    raw = json.loads(CACHE.read_text(encoding="utf-8"))["data"]
    print("载入缓存 %d 只" % len(raw))
    codes = sorted(raw.keys())
    CODES.write_text(json.dumps(codes, ensure_ascii=False), encoding="utf-8")
    print("写出代码表 %d 个" % len(codes))

    by_date = defaultdict(dict)
    for code, bars in raw.items():
        for b in bars:
            if b[0] >= "2026-01-01":
                by_date[b[0]][code] = b
    dates = sorted(by_date)
    prev = {}
    daily = []
    for d in dates:
        day = by_date[d]
        up = dn = touch = sealed = ups = downs = 0
        lu = []
        chgs = []
        for code, b in day.items():
            pc = prev.get(code)
            if not pc:
                continue
            lim = limit_of(code)
            chg = (b[4] / pc - 1) * 100
            hchg = (b[2] / pc - 1) * 100
            chgs.append(chg)
            if chg > 0:
                ups += 1
            elif chg < 0:
                downs += 1
            if chg >= lim:
                up += 1
                lu.append(code)
            elif chg <= -lim:
                dn += 1
            if hchg >= lim:
                touch += 1
                if chg >= lim:
                    sealed += 1
        for code, b in day.items():
            prev[code] = b[4]
        lu_set = set(lu)
        lb2 = len([c for c in lu if c in prev["_lu"]]) if "_lu" in prev else 0
        prev["_lu"] = lu_set
        daily.append({"d": d, "up": up, "dn": dn, "touch": touch, "sealed": sealed,
                      "ups": ups, "downs": downs, "lb2": lb2, "lu": sorted(lu),
                      "br": round((touch - sealed) / touch, 4) if touch else 0.0,
                      "eq": round(sum(chgs) / len(chgs), 4) if chgs else 0.0})
    # 最高连板高度
    for i, rec in enumerate(daily):
        h = 1
        for c in rec["lu"]:
            n = 1
            for j in range(i - 1, max(-1, i - 12), -1):
                if c in set(daily[j]["lu"]):
                    n += 1
                else:
                    break
            h = max(h, n)
        rec["lbh"] = h if rec["lu"] else 0
    # 裁剪：只留最近 90 天的涨停名单（体积）
    for rec in daily[:-90]:
        rec.pop("lu", None)
    HIST.write_text(json.dumps({"updated": daily[-1]["d"], "daily": daily},
                               ensure_ascii=False), encoding="utf-8")
    print("种子历史 %d 个交易日（%s ~ %s）" % (len(daily), daily[0]["d"], daily[-1]["d"]))


# ─────────────────────── 每日更新 ───────────────────────
def do_update():
    obj = json.loads(HIST.read_text(encoding="utf-8")) if HIST.exists() else {"daily": []}
    daily = obj["daily"]
    codes = json.loads(CODES.read_text(encoding="utf-8"))
    print("已有 %d 个交易日，开始拉取最新行情（%d 只）…" % (len(daily), len(codes)))
    quotes = fetch_sina(codes)
    if not quotes:
        raise SystemExit("行情抓取失败，未获取到任何数据")
    print("获取 %d 只行情" % len(quotes))
    d = ""
    for q in quotes.values():
        if q.get("date"):
            d = q["date"]
            break
    if not d:
        raise SystemExit("行情里没有日期字段")
    print("行情日期 %s" % d)

    prev_lu = set()
    for rec in reversed(daily):
        if rec.get("lu"):
            prev_lu = set(rec["lu"])
            break
    st = day_stats(quotes, prev_lu)
    rec = {"d": d, "up": st["up"], "dn": st["dn"], "touch": st["touch"],
           "sealed": st["sealed"], "ups": st["ups"], "downs": st["downs"],
           "lb2": st["lb2"], "lu": sorted(st["lu"]),
           "br": round((st["touch"] - st["sealed"]) / st["touch"], 4) if st["touch"] else 0.0,
           "eq": st["eq"]}
    h = 0
    for c in rec["lu"]:
        h = max(h, chain_height(c, daily))
    rec["lbh"] = h
    daily = [x for x in daily if x["d"] != d] + [rec]
    daily.sort(key=lambda x: x["d"])
    for x in daily[:-90]:
        x.pop("lu", None)
    obj = {"updated": d, "daily": daily}
    HIST.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    print("已更新 %s：涨停 %d / 跌停 %d / 连板 %d / 最高 %d 板 / 炸板率 %.1f%%"
          % (d, rec["up"], rec["dn"], rec["lb2"], rec["lbh"], rec["br"] * 100))


# ─────────────────────── 页面生成 ───────────────────────
CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0e1116;color:#e6e9ef;font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif;
     padding:22px 18px 60px;line-height:1.6}
.wrap{max-width:1080px;margin:0 auto}
h1{font-size:22px;font-weight:700;letter-spacing:.5px}
.sub{color:#8b95a5;font-size:12.5px;margin-top:6px}
.grid{display:grid;gap:12px}
.g6{grid-template-columns:repeat(6,1fr)}
.g3{grid-template-columns:repeat(3,1fr)}
.card{background:#161b23;border:1px solid #232a36;border-radius:12px;padding:14px 16px}
.card .k{color:#8b95a5;font-size:12px}
.card .v{font-size:24px;font-weight:700;margin-top:4px;font-variant-numeric:tabular-nums}
.card .n{color:#6b7484;font-size:11px;margin-top:2px}
.up{color:#f0544f}.dn{color:#22a06b}.flat{color:#8b95a5}
.hero{display:flex;gap:20px;align-items:center;flex-wrap:wrap}
.emo{font-size:58px;font-weight:800;line-height:1;font-variant-numeric:tabular-nums}
.tag{display:inline-block;padding:5px 14px;border-radius:999px;font-size:14px;font-weight:700}
.sec{margin-top:22px}
.sec h2{font-size:15px;color:#c6cedb;font-weight:700;margin-bottom:10px;
        border-left:3px solid #3b82f6;padding-left:9px}
.adv{background:#141a22;border-left:4px solid #3b82f6;border-radius:8px;padding:12px 15px;font-size:13.5px}
.adv b{font-size:15px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{padding:7px 8px;border-bottom:1px solid #232a36;text-align:left}
th{color:#8b95a5;font-weight:600;background:#141922}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.foot{margin-top:28px;color:#6b7484;font-size:11.5px;border-top:1px solid #232a36;padding-top:12px}
.lg{display:flex;gap:16px;font-size:11.5px;color:#8b95a5;margin-bottom:6px;flex-wrap:wrap}
.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}
"""


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def svg_line(emos, dates, w=1020, h=220):
    """情绪值走势（含 25 / 75 参考线）"""
    pairs = [(d, e) for d, e in zip(dates, emos) if e is not None]
    n = len(pairs)
    if n < 2:
        return "<p style='color:#8b95a5'>数据不足</p>"
    L, R, T, B = 44, 16, 16, 26
    pw, ph = w - L - R, h - T - B
    step = pw / float(n - 1)

    def X(i):
        return L + i * step

    def Y(v):
        return T + (100 - v) / 100.0 * ph

    out = ['<svg viewBox="0 0 %d %d" width="100%%" height="%d">' % (w, h, h)]
    for v, lb, col in ((75, "75 高潮线", "#c0392b"), (25, "25 冰点线", "#2a6f97")):
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" '
                   'stroke-width="1" stroke-dasharray="5 4" opacity="0.55"/>' % (L, Y(v), w - R, Y(v), col))
        out.append('<text x="%d" y="%.1f" font-size="10" fill="%s" text-anchor="end">%s</text>'
                   % (L - 6, Y(v) + 3.5, col, lb))
    cur = [(X(i), Y(e), e) for i, (_, e) in enumerate(pairs)]
    for i in range(len(cur) - 1):
        x1, y1, e1 = cur[i]
        x2, y2, e2 = cur[i + 1]
        col = "#2a6f97" if e2 < 25 else ("#c0392b" if e2 >= 55 else "#8a6d3b")
        out.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="2"/>'
                   % (x1, y1, x2, y2, col))
    out.append('<circle cx="%.1f" cy="%.1f" r="4" fill="#fff" stroke="#3b82f6" stroke-width="2"/>'
               % (cur[-1][0], cur[-1][1]))
    out.append('<text x="%.1f" y="%.1f" font-size="11" fill="#e6e9ef" text-anchor="middle">%.1f</text>'
               % (cur[-1][0], cur[-1][1] - 9, cur[-1][2]))
    for k in range(0, n, max(1, n // 6)):
        out.append('<text x="%.1f" y="%d" font-size="9.5" fill="#6b7484" text-anchor="middle">%s</text>'
                   % (X(k), h - 8, pairs[k][0][5:]))
    out.append("</svg>")
    return "".join(out)


def svg_bars(daily, w=1020, h=190, days=30):
    """最近 N 日涨停/跌停家数"""
    seg = daily[-days:]
    n = len(seg)
    if n < 2:
        return ""
    mx = max(max(d["up"] for d in seg), max(d["dn"] for d in seg), 10)
    L, R, T, B = 44, 16, 14, 26
    pw, ph = w - L - R, h - T - B
    cw = pw / float(n)
    out = ['<svg viewBox="0 0 %d %d" width="100%%" height="%d">' % (w, h, h)]
    for i, d in enumerate(seg):
        x = L + i * cw
        hu = d["up"] / mx * ph
        hd = d["dn"] / mx * ph
        out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="#f0544f" opacity="0.9"/>'
                   % (x + cw * 0.12, T + ph - hu, cw * 0.36, hu))
        out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="#22a06b" opacity="0.9"/>'
                   % (x + cw * 0.52, T + ph - hd, cw * 0.36, hd))
    out.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#232a36"/>' % (L, T + ph, w - R, T + ph))
    out.append('<text x="%d" y="%.1f" font-size="10" fill="#6b7484" text-anchor="end">%d</text>'
               % (L - 6, T + 10, mx))
    for k in range(0, n, max(1, n // 6)):
        out.append('<text x="%.1f" y="%d" font-size="9.5" fill="#6b7484" text-anchor="middle">%s</text>'
                   % (L + k * cw + cw / 2, h - 8, seg[k]["d"][5:]))
    out.append("</svg>")
    return "".join(out)


def phase_color(p):
    return {"冰点": "#2a6f97", "启动": "#2e8b57", "发酵": "#c0392b",
            "高潮": "#8e2b22", "退潮": "#128a6f", "震荡": "#8a6d3b"}.get(p, "#8b95a5")


def render_html(obj):
    daily = obj["daily"]
    emos = emo_series(daily)
    last = daily[-1]
    e = emos[-1] if emos else None
    ph = phase_of(emos, len(daily) - 1)
    adv, note = PHASE_ADVICE.get(ph, ("—", ""))
    d = last["d"]
    br = last["br"] * 100
    ratio = (last["ups"] / last["downs"]) if last["downs"] else last["ups"]

    recs = list(reversed(daily[-20:]))
    rows = []
    for i, r in enumerate(recs):
        idx = len(daily) - 1 - i
        ev = emos[idx]
        p = phase_of(emos, idx)
        rows.append(
            "<tr><td>%s</td><td class='num up'>%d</td><td class='num dn'>%d</td>"
            "<td class='num'>%d</td><td class='num'>%d</td><td class='num'>%.1f%%</td>"
            "<td class='num'>%s</td><td><span style='color:%s'>%s</span></td></tr>"
            % (r["d"], r["up"], r["dn"], r["lb2"], r["lbh"], r["br"] * 100,
               ("%.1f" % ev) if ev is not None else "—", phase_color(p), p))

    dist = defaultdict(int)
    for i in range(len(daily)):
        if emos[i] is not None:
            dist[phase_of(emos, i)] += 1
    dist_rows = "".join(
        "<tr><td><span style='color:%s'>■</span> %s</td><td class='num'>%d 天</td></tr>"
        % (phase_color(k), k, v)
        for k, v in sorted(dist.items(), key=lambda x: -x[1]) if k != "数据不足")

    ecolor = phase_color(ph)
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>A 股情绪看板 · {esc(d)}</title><style>{CSS}</style></head><body>
<div class="wrap">
  <h1>A 股情绪看板</h1>
  <div class="sub">数据日期 <b style="color:#e6e9ef">{esc(d)}</b>　·　数据来源：新浪财经行情（日线快照）
  　·　情绪值口径与《情绪周期与龙头实战》第 17 册一致　·　仅供参考，不构成投资建议</div>

  <div class="sec"><div class="card">
    <div class="hero">
      <div>
        <div class="k">今日情绪值（0~100）</div>
        <div class="emo" style="color:{ecolor}">{'%.1f' % e if e is not None else '—'}</div>
      </div>
      <div style="flex:1;min-width:260px">
        <span class="tag" style="background:{ecolor}22;color:{ecolor};border:1px solid {ecolor}66">{esc(ph)}</span>
        <div style="margin-top:10px;font-size:13.5px;color:#c6cedb">{esc(note)}</div>
      </div>
    </div>
  </div></div>

  <div class="sec grid g6">
    <div class="card"><div class="k">涨停家数</div><div class="v up">{last['up']}</div></div>
    <div class="card"><div class="k">跌停家数</div><div class="v dn">{last['dn']}</div></div>
    <div class="card"><div class="k">连板家数</div><div class="v">{last['lb2']}</div><div class="n">≥2 连板</div></div>
    <div class="card"><div class="k">最高连板</div><div class="v">{last['lbh']}</div><div class="n">板</div></div>
    <div class="card"><div class="k">炸板率</div><div class="v">{br:.1f}%</div><div class="n">触及未封住</div></div>
    <div class="card"><div class="k">涨/跌家数</div><div class="v">{last['ups']}/{last['downs']}</div>
      <div class="n">比值 {ratio:.2f}</div></div>
  </div>

  <div class="sec"><h2>情绪值走势（近 60 个交易日）</h2>
    <div class="card"><div class="lg">
      <span><span class="sw" style="background:#2a6f97"></span>冰点 &lt;25</span>
      <span><span class="sw" style="background:#8a6d3b"></span>中性 25~55</span>
      <span><span class="sw" style="background:#c0392b"></span>发酵/高潮 ≥55</span>
      <span>虚线：25 冰点线 · 75 高潮线</span></div>
      {svg_line(emos[-60:], [x['d'] for x in daily[-60:]])}</div></div>

  <div class="sec"><h2>涨停 / 跌停家数（近 30 个交易日）</h2>
    <div class="card"><div class="lg">
      <span><span class="sw" style="background:#f0544f"></span>涨停家数</span>
      <span><span class="sw" style="background:#22a06b"></span>跌停家数</span></div>
      {svg_bars(daily)}</div></div>

  <div class="sec"><h2>今天该做什么</h2>
    <div class="adv"><b style="color:{ecolor}">{esc(adv)}</b><br>
      <span style="color:#8b95a5">判定为「{esc(ph)}」阶段。完整规则见《情绪周期与龙头实战》第 1、2、7 章；
      八条下单检查表见第 8 章。</span></div></div>

  <div class="sec grid g3" style="align-items:start">
    <div class="card" style="grid-column:span 2">
      <h2 style="margin-bottom:8px">近 20 个交易日明细</h2>
      <table><tr><th>日期</th><th class="num">涨停</th><th class="num">跌停</th>
      <th class="num">连板</th><th class="num">最高板</th><th class="num">炸板率</th>
      <th class="num">情绪值</th><th>阶段</th></tr>{''.join(rows)}</table>
    </div>
    <div class="card">
      <h2 style="margin-bottom:8px">历史阶段分布</h2>
      <table>{dist_rows}</table>
      <div class="n" style="margin-top:10px;color:#6b7484">
      统计区间 {esc(daily[0]['d'])} ~ {esc(d)}，共 {len(daily)} 个交易日。</div>
    </div>
  </div>

  <div class="foot">
    情绪值 ＝ 50% × 涨停家数分位 ＋ 30% × 连板家数分位 ＋ 20% ×（100 − 炸板率分位），
    分位窗口为过去 60 个交易日，全部只用历史数据、不含未来函数。<br>
    涨停判定按各板块限幅近似（主板 9.6%／创业板与科创板 19.6%／ST 4.8%／北交所 29.6%），
    与交易所口径可能存在个别差异。<br>
    本页为个人学习工具，<b>不构成任何投资建议</b>；历史统计不预测未来，短线交易风险极高。<br>
    生成时间 {esc(datetime.now().strftime('%Y-%m-%d %H:%M'))}　·　数据更新脚本 <code>build_dashboard.py</code>
  </div>
</div></body></html>
"""


def do_render():
    obj = json.loads(HIST.read_text(encoding="utf-8"))
    (ROOT / "index.html").write_text(render_html(obj), encoding="utf-8")
    d = obj["daily"]
    emos = emo_series(d)
    ph = phase_of(emos, len(d) - 1)
    print("页面已生成：index.html（%s，%d 个交易日，最新阶段：%s）"
          % (d[-1]["d"], len(d), ph))


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--offline", action="store_true")
    a = ap.parse_args()
    if a.init:
        do_init()
    if not a.offline and not a.init:
        do_update()
    do_render()


if __name__ == "__main__":
    main()
