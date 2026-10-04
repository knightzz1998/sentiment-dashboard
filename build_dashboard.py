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
import subprocess
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
SWING = DATA / "swing.json"
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


# ─────────────────── 东财涨停池 / 个股日线（用于明细与观察池） ───────────────────
EM_UT = "7eea3edcaed734bea9cbfc24409ed989"


def fetch_zt_pool(date_str):
    """东财涨停池（date_str 形如 20260930）。⚠️ 必须带 date 参数，否则返回空。"""
    if not date_str:
        return []
    url = ("https://push2ex.eastmoney.com/getTopicZTPool?ut=" + EM_UT +
           "&dpt=wz.ztzt&Pageindex=0&pagesize=300&sort=fbt:asc&date=" + date_str)
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                              "Referer": "https://quote.eastmoney.com/"})
    for attempt in range(3):
        try:
            d = json.loads(urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore"))
            return (d.get("data") or {}).get("pool") or []
        except Exception:
            time.sleep(1.2 + attempt)
    return []


def fetch_kline(code, datalen=70):
    """新浪日线（用于算 MA60 位置）"""
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=" + sina_symbol(code) +
           "&scale=240&ma=no&datalen=" + str(datalen))
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                              "Referer": "https://finance.sina.com.cn"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=18).read().decode("utf-8", "ignore"))
    except Exception:
        return []


def ma60_dev(code):
    """收盘价相对 MA60 的偏离（%）；数据不足返回 None"""
    bars = fetch_kline(code)
    closes = [float(b["close"]) for b in bars if b.get("close")]
    if len(closes) < 60:
        return None
    ma = sum(closes[-60:]) / 60.0
    return (closes[-1] / ma - 1) * 100 if ma else None


def board_type_of(row, q):
    """板型判定（口径与《情绪周期与龙头实战》4.1 完全一致）"""
    if not q or q["prev"] <= 0:
        return "换手板"
    lim = limit_of(row["c"], row.get("n", ""))
    try:
        ochg = (q["open"] / q["prev"] - 1) * 100
        lchg = (q["low"] / q["prev"] - 1) * 100
    except ZeroDivisionError:
        return "换手板"
    if ochg >= lim - 0.3:                       # 开盘即涨停
        return "一字板" if lchg >= lim - 0.3 else "T字板"
    if lchg > 0.5:
        return "强势换手板"                      # 全天都在红盘，一气拉到涨停
    if lchg > -3.0:
        return "普通换手板"
    return "弱势/反包板"                         # 先绿后红，V 型拉板


def build_detail(rows, quotes, daily, date_str):
    """当日明细：连板梯队 / 板型 / 题材热度 / 封板质量 / 观察池 / 赚钱效应"""
    d = {"date": date_str, "ladder": {}, "ladder_names": {}, "boards": {},
         "themes": [], "seal_top": [], "watch": [], "n_pool": len(rows),
         "prev_lu_ret": None, "prev_lu_win": None, "prev_lu_n": 0}

    # ① 连板梯队（几进几）
    for r in rows:
        k = min(int(r.get("lbc") or 1), 6)
        d["ladder"][str(k)] = d["ladder"].get(str(k), 0) + 1
        d["ladder_names"].setdefault(str(k), []).append(
            {"code": r["c"], "name": r.get("n", ""), "hybk": r.get("hybk", "")})

    # ② 板型分布
    for r in rows:
        bt = board_type_of(r, quotes.get(r["c"]))
        d["boards"][bt] = d["boards"].get(bt, 0) + 1

    # ③ 题材热度（按东财行业板块归集涨停家数）
    th = defaultdict(int)
    for r in rows:
        hb = (r.get("hybk") or "其他").split("-")[0]
        th[hb] += 1
    d["themes"] = sorted([[k, v] for k, v in th.items()], key=lambda x: -x[1])[:12]

    # ④ 封板质量榜（封成比 = 封单额 ÷ 成交额）
    seal = []
    for r in rows:
        amt = r.get("amount") or 0
        fund = r.get("fund") or 0
        if amt <= 0:
            continue
        seal.append({"code": r["c"], "name": r.get("n", ""), "lbc": r.get("lbc") or 1,
                     "ratio": round(fund / amt, 1), "fund": round(fund / 1e8, 2),
                     "zbc": r.get("zbc") or 0, "hybk": r.get("hybk", "")})
    d["seal_top"] = sorted(seal, key=lambda x: -x["ratio"])[:10]

    # ⑤ 赚钱效应：昨日涨停股今日表现
    prev_lu = set()
    for rec in reversed(daily):
        if rec["d"] < date_str and rec.get("lu"):
            prev_lu = set(rec["lu"])
            break
    rets = []
    for c in prev_lu:
        q = quotes.get(c)
        if not q or q["prev"] <= 0:
            continue
        rets.append((q["now"] / q["prev"] - 1) * 100)
    if rets:
        d["prev_lu_n"] = len(rets)
        d["prev_lu_ret"] = round(sum(rets) / len(rets), 2)
        d["prev_lu_win"] = round(sum(1 for x in rets if x > 0) / len(rets) * 100, 1)

    # ⑥ 观察池：按《情绪周期与龙头实战》第 8 章检查表中可自动化的四条
    theme_n = dict(d["themes"])
    cands = []
    for r in rows:
        lbc = int(r.get("lbc") or 1)
        zbc = int(r.get("zbc") or 0)
        amt = r.get("amount") or 0
        fund = r.get("fund") or 0
        ratio = (fund / amt) if amt else 0
        hb = (r.get("hybk") or "其他").split("-")[0]
        cond = {
            "连板数 ≤3": lbc <= 3,
            "未开板（≤1 次）": zbc <= 1,
            "封成比 >10": ratio > 10,
            "同题材涨停 ≥3 家": theme_n.get(hb, 0) >= 3,
        }
        hit = sum(1 for v in cond.values() if v)
        if hit >= 3:
            cands.append({"code": r["c"], "name": r.get("n", ""), "lbc": lbc,
                          "zbc": zbc, "ratio": round(ratio, 1),
                          "fund": round(fund / 1e8, 2), "hybk": hb,
                          "amount": round(amt / 1e8, 2), "cond": cond, "hit": hit})
    cands.sort(key=lambda x: (-x["hit"], -x["ratio"]))
    d["n_screen"] = len(cands)
    # 只对候选（最多 12 只）补 MA60 位置，并把「位置」并入条件命中
    for c in cands[:12]:
        dev = ma60_dev(c["code"])
        c["dev60"] = round(dev, 1) if dev is not None else None
        c["pos_ok"] = bool(dev is not None and dev < 25)
        c["cond"]["位置 &lt;MA60+25%"] = c["pos_ok"]
        c["hit"] = sum(1 for v in c["cond"].values() if v)
    # 位置合格的排前面（位置是第 8 章检查表里优先级最高的一条）
    d["watch"] = sorted(cands[:12], key=lambda x: (not x["pos_ok"], -x["hit"], -x["ratio"]))
    d["watch_pos_ok"] = sum(1 for x in d["watch"] if x["pos_ok"])
    return d


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
        if rec["d"] < d and rec.get("lu"):        # 必须是「上一交易日」，排除当日自己
            prev_lu = set(rec["lu"])
            break
    st = day_stats(quotes, prev_lu)
    rec = {"d": d, "up": st["up"], "dn": st["dn"], "touch": st["touch"],
           "sealed": st["sealed"], "ups": st["ups"], "downs": st["downs"],
           "lb2": st["lb2"], "lu": sorted(st["lu"]),
           "br": round((st["touch"] - st["sealed"]) / st["touch"], 4) if st["touch"] else 0.0,
           "eq": st["eq"]}
    # 先剔除同日旧记录，否则回溯连板时会把当日自己也算一遍（导致高度 +1）
    daily = [x for x in daily if x["d"] != d]
    h = 0
    for c in rec["lu"]:
        h = max(h, chain_height(c, daily))
    rec["lbh"] = h
    daily = daily + [rec]
    daily.sort(key=lambda x: x["d"])
    for x in daily[:-90]:
        x.pop("lu", None)
    obj = {"updated": d, "daily": daily}
    HIST.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    print("已更新 %s：涨停 %d / 跌停 %d / 连板 %d / 最高 %d 板 / 炸板率 %.1f%%"
          % (d, rec["up"], rec["dn"], rec["lb2"], rec["lbh"], rec["br"] * 100))

    # —— 当日明细：连板梯队 / 板型 / 题材热度 / 封板质量 / 赚钱效应 / 观察池 ——
    rows = fetch_zt_pool(d.replace("-", ""))
    print("东财涨停池 %d 条" % len(rows))
    if rows:
        detail = build_detail(rows, quotes, daily, d)
        (DATA / "today.json").write_text(json.dumps(detail, ensure_ascii=False), encoding="utf-8")
        print("  连板梯队 %s" % detail["ladder"])
        print("  板型分布 %s" % detail["boards"])
        print("  题材 TOP5 %s" % detail["themes"][:5])
        print("  观察池 %d 只" % len(detail["watch"]))
        if detail["prev_lu_ret"] is not None:
            print("  赚钱效应：昨日涨停股今日均值 %+.2f%%（上涨 %.1f%%，n=%d）"
                  % (detail["prev_lu_ret"], detail["prev_lu_win"], detail["prev_lu_n"]))
    else:
        print("  涨停池为空，跳过明细（东财接口需要 date 参数，或该日无数据）")


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


def render_detail(det):
    """当日明细区块：赚钱效应 / 连板梯队 / 板型 / 题材热度 / 封板质量 / 观察池"""
    if not det:
        return ""
    h = []
    nm = {"1": "首板", "2": "2 连板", "3": "3 连板", "4": "4 连板",
          "5": "5 连板", "6": "6 板及以上"}
    # ── 赚钱效应 + 连板梯队 ──
    pr = det.get("prev_lu_ret")
    if pr is None:
        hero, hc = "<div class='v flat'>—</div><div class='n'>无样本</div>", "#8b95a5"
    else:
        hc = "#f0544f" if pr > 0 else "#22a06b"
        hero = ("<div class='v' style='color:%s'>%+.2f%%</div>"
                "<div class='n'>上涨占比 %.1f%%　样本 %d 只</div>"
                % (hc, pr, det["prev_lu_win"], det["prev_lu_n"]))
    ladder = det.get("ladder", {})
    lrows = []
    tot = sum(ladder.values()) or 1
    for k in sorted(ladder, key=lambda x: int(x)):
        names = det.get("ladder_names", {}).get(k, [])[:3]
        rep = "、".join("%s(%s)" % (x["name"], x["code"]) for x in names)
        more = "等" if len(det.get("ladder_names", {}).get(k, [])) > 3 else ""
        bar = int(ladder[k] / tot * 100)
        lrows.append(
            "<tr><td>%s</td><td class='num'>%d</td>"
            "<td><div style='background:#232a36;border-radius:3px;height:8px'>"
            "<div style='background:#c0392b;height:8px;border-radius:3px;width:%d%%'></div></div></td>"
            "<td style='color:#8b95a5'>%s%s</td></tr>"
            % (nm.get(k, k + " 板"), ladder[k], max(bar, 3), rep, more))
    h.append(
        "<div class='sec grid g3' style='align-items:start'>"
        "<div class='card'><div class='k'>赚钱效应　昨日涨停股今日表现</div>%s"
        "<div class='n' style='margin-top:8px;color:#6b7484'>"
        "这是短线的核心温度计：昨天封板的票今天有没有溢价。为负说明接力在亏钱。</div></div>"
        "<div class='card' style='grid-column:span 2'><h2 style='margin-bottom:8px'>连板梯队（几进几）</h2>"
        "<table><tr><th style='width:90px'>层级</th><th class='num' style='width:60px'>家数</th>"
        "<th style='width:26%%'>占比</th><th>代表个股</th></tr>%s</table></div></div>"
        % (hero, "".join(lrows)))

    # ── 板型 + 题材热度 ──
    boards = det.get("boards", {})
    bt_rows = "".join(
        "<tr><td>%s</td><td class='num'>%d</td><td class='num'>%.0f%%</td></tr>"
        % (k, v, v / max(sum(boards.values()), 1) * 100)
        for k, v in sorted(boards.items(), key=lambda x: -x[1]))
    th = det.get("themes", [])
    mx = th[0][1] if th else 1
    th_rows = "".join(
        "<tr><td>%s</td><td class='num'>%d</td>"
        "<td><div style='background:#232a36;border-radius:3px;height:8px'>"
        "<div style='background:#e0a458;height:8px;border-radius:3px;width:%d%%'></div></div></td></tr>"
        % (k, v, max(int(v / mx * 100), 4)) for k, v in th[:12])
    h.append(
        "<div class='sec grid' style='grid-template-columns:0.8fr 1.2fr'>"
        "<div class='card'><h2 style='margin-bottom:8px'>涨停板型分布</h2>"
        "<table><tr><th>板型</th><th class='num'>家数</th><th class='num'>占比</th></tr>%s</table>"
        "<div class='n' style='margin-top:8px;color:#6b7484'>"
        "一字板买不到；弱势/反包板在 20 日尺度上最差（见第 17 册 4.1）。</div></div>"
        "<div class='card'><h2 style='margin-bottom:8px'>题材热度（按涨停家数归集）</h2>"
        "<table><tr><th>行业 / 题材</th><th class='num' style='width:70px'>家数</th>"
        "<th style='width:34%%'>热度</th></tr>%s</table>"
        "<div class='n' style='margin-top:8px;color:#6b7484'>"
        "家数最多的方向才可能成为主线；只有一天的只是「当日热点」。</div></div></div>"
        % (bt_rows, th_rows))

    # ── 封板质量榜 ──
    seal = det.get("seal_top", [])
    srows = "".join(
        "<tr><td>%s</td><td>%s</td><td class='num'>%d</td>"
        "<td class='num' style='color:#c0392b'>%.1f</td><td class='num'>%.2f</td>"
        "<td class='num'>%d</td><td style='color:#8b95a5'>%s</td></tr>"
        % (x["code"], x["name"], x["lbc"], x["ratio"], x["fund"], x["zbc"], x["hybk"])
        for x in seal)
    h.append(
        "<div class='sec'><h2>封板质量榜（封成比 = 封单额 ÷ 成交额）</h2>"
        "<div class='card'><table><tr><th>代码</th><th>名称</th><th class='num'>连板</th>"
        "<th class='num'>封成比</th><th class='num'>封单(亿)</th><th class='num'>开板次数</th>"
        "<th>行业</th></tr>%s</table>"
        "<div class='n' style='margin-top:8px;color:#6b7484'>"
        "封成比 &gt;10 且开板 0 次＝封得结实；封成比 &lt;3 或反复开板＝分歧大。"
        "这是「当日强度」的描述，不能单独用来预测次日。</div></div></div>" % srows)

    # ── 观察池 ──
    watch = det.get("watch", [])
    if watch:
        wrows = []
        for w in watch:
            dev = w.get("dev60")
            devs = ("%+.1f%%" % dev) if dev is not None else "—"
            devc = "#f0544f" if (dev is not None and dev > 25) else "#e6e9ef"
            hits = "".join(
                "<span style='color:%s;margin-right:6px'>%s%s</span>"
                % ("#22a06b" if v else "#6b7484", "✓" if v else "✗", k)
                for k, v in w["cond"].items())
            wrows.append(
                "<tr><td>%s</td><td>%s</td><td class='num'>%d</td>"
                "<td class='num'>%.1f</td><td class='num'>%.2f</td>"
                "<td class='num' style='color:%s'>%s</td>"
                "<td style='color:#8b95a5'>%s</td><td style='font-size:11.5px'>%s</td></tr>"
                % (w["code"], w["name"], w["lbc"], w["ratio"], w["fund"],
                   devc, devs, w["hybk"], hits))
        stat_line = (
            "当日涨停 <b>%d</b> 只 → 通过「板数≤3 / 未开板 / 同题材≥3 家」初筛 <b>%d</b> 只 → "
            "其中<b style='color:#f0ad4e'>位置合格（MA60 偏离 &lt;+25%%）的只有 %d 只</b>。"
            "位置是检查表里优先级最高的一条：偏离过大直接放弃，不论其他条件多好。"
            % (det.get("n_pool", 0), det.get("n_screen", 0), det.get("watch_pos_ok", 0)))
        h.append(
            "<div class='sec'><h2>观察池（按第 17 册第 8 章检查表筛选）</h2>"
            "<div class='card'><div style='font-size:12.5px;color:#c6cedb;margin-bottom:10px'>"
            + stat_line + "</div>"
            "<table><tr><th>代码</th><th>名称</th><th class='num'>板数</th>"
            "<th class='num'>封成比</th><th class='num'>封单(亿)</th><th class='num'>MA60偏离</th>"
            "<th>行业</th><th>条件命中</th></tr>" + "".join(wrows) + "</table>"
            "<div class='warning' style='margin-top:12px;border-left:3px solid #f0ad4e;"
            "background:#1d1a12;padding:11px 14px;border-radius:8px;font-size:12.5px;color:#d8c9a8'>"
            "<b>这不是推荐，也不是买入信号。</b>它只是「符合几条已知规则」的筛选结果。"
            "任何一条不满足，都应该放弃，而不是打折执行。<br>"
            "历史统计显示：没有任何单一信号值得「看到就买」；28 种可量化战法里，"
            "扣掉成本后只有 4 种毛超额为正，且幅度都在噪声级。</div></div></div>")
    return "".join(h)


def do_swing():
    """调用波段选股器（腾讯/东财前复权）→ data/swing.json + data/stage1_log.json"""
    script = ROOT / "swing_screen.py"
    if not script.exists():
        print("未找到 swing_screen.py，跳过波段选股")
        return
    try:
        proc = subprocess.run([sys.executable, str(script)], cwd=str(ROOT),
                              capture_output=True, text=True, timeout=1200, check=False)
    except Exception as e:
        print("波段选股运行失败：%s: %s" % (type(e).__name__, e))
        return
    out = (proc.stdout or "").strip()
    if out:
        for line in out.splitlines():
            print("  [波段] " + line)
    if proc.returncode != 0:
        print("  [波段] 退出码 %s；stderr 末尾：%s"
              % (proc.returncode, (proc.stderr or "")[-300:]))


def render_swing():
    """波段闸门 + 候选 + 阶段 1 进度（数据来自 swing_screen.py）"""
    if not SWING.exists():
        return ""
    try:
        s = json.loads(SWING.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not s or not s.get("date"):
        return ""

    go = bool(s.get("gate_open"))
    gc = "#22a06b" if go else "#f0544f"
    fun = s.get("funnel") or {}
    funnel = " → ".join("%s <b>%s</b>" % (esc(k), format(v, ",")) for k, v in fun.items())

    main = s.get("main") or []
    rows = []
    for c in main[:12]:
        rows.append(
            "<tr><td>%s</td><td>%s</td><td class='num'>%.2f</td>"
            "<td class='num'>%+.2f%%</td><td class='num'>%.2f%%</td>"
            "<td class='num'>%+.2f%%</td><td class='num'>%.2f</td>"
            "<td class='num'>%d</td></tr>"
            % (esc(c.get("code", "")), esc(c.get("name", "")), c.get("close", 0),
               c.get("dev60", 0), c.get("disp", 0), c.get("buf", 0),
               c.get("amt", 0), c.get("score", 0)))
    if not rows:
        rows.append("<tr><td colspan='8' style='color:#8b95a5'>"
                    "今天没有通过八级条件的候选——这是正常结果，不是失败（漏斗 0.6% 本就很窄）</td></tr>")
    more = ""
    if len(main) > 12:
        more = ("<div class='n' style='margin-top:6px;color:#6b7484'>另有 %d 只未列出，"
                "完整清单见 data/swing.json</div>" % (len(main) - 12))

    np_ = len(s.get("need_perm") or [])
    nst = len(s.get("st") or [])

    st = s.get("stage1") or {}
    done, tgt = st.get("done", 0), st.get("target", 20)
    pct = min(100.0, done / float(tgt) * 100) if tgt else 0
    lrows = []
    for r in reversed(st.get("rows") or []):
        g = bool(r.get("gate"))
        lrows.append(
            "<tr><td>%s</td><td>%s</td><td class='num'>%s</td>"
            "<td><span style='color:%s'>%s</span></td><td class='num'>%d</td></tr>"
            % (esc(r.get("d", "")), esc(r.get("regime", "—")),
               ("%+.2f%%" % r["chg20"]) if r.get("chg20") is not None else "—",
               "#22a06b" if g else "#f0544f", "开放" if g else "关闭", r.get("n", 0)))

    return f"""
  <div class="sec"><h2>波段闸门与候选（第 19／20 册口径）</h2>
    <div class="adv" style="border-left-color:{gc}">
      <b style="color:{gc}">闸门：{'开放' if go else '关闭'}</b>
      <span style="color:#c6cedb">　{esc(s.get('gate_reason', ''))}</span><br>
      <span style="color:#8b95a5">筛选漏斗（八级硬条件）：{funnel}</span>
    </div>
  </div>

  <div class="sec grid g3">
    <div class="card"><div class="k">大盘环境（等权指数 20 日）</div>
      <div class="v" style="color:{gc}">{esc(s.get('regime', '—'))}</div>
      <div class="n">{'%+.2f%%' % s['chg20'] if s.get('chg20') is not None else '—'}</div></div>
    <div class="card"><div class="k">主板候选（3 万可执行）</div>
      <div class="v">{len(main)}</div><div class="n">只</div></div>
    <div class="card"><div class="k">需权限 / 已剔除</div>
      <div class="v" style="font-size:19px">{np_} / {nst}</div>
      <div class="n">创业科创北交 / ST</div></div>
  </div>

  <div class="sec"><h2>候选清单（主板，按复合评分排序）</h2>
    <div class="card">
      <table><tr><th>代码</th><th>名称</th><th class="num">收盘</th>
      <th class="num">高于MA60</th><th class="num">发散度</th><th class="num">MA20缓冲</th>
      <th class="num">20日均额(亿)</th><th class="num">评分</th></tr>{''.join(rows)}</table>
      {more}
      <div class="n" style="margin-top:8px;color:#6b7484">
      初始止损＝收盘 × 0.90（移动止损）；加仓参考位＝MA20。
      <b style="color:#f0544f">候选≠推荐≠买入信号</b>：入场信号本身的超额只有 +0.01%，
      真正的价值是「排除不该做的票」。开盘前还要核对第 19 册第 8 章的十项清单。</div>
    </div>
  </div>

  <div class="sec"><h2>阶段 1 · 空跑记录（每日自动追加）</h2>
    <div class="card">
      <div style="font-size:13.5px;color:#c6cedb;margin-bottom:8px">
        已完成 <b style="color:#3b82f6">{done}</b> / {tgt} 个交易日　·　
        其中闸门开放 <b style="color:#22a06b">{st.get('open_days', 0)}</b> 天　·　
        <span style="color:#8b95a5">记录满 20 天后，回看「闸门开过几天、那几天候选有哪些」</span></div>
      <div style="height:6px;background:#232a36;border-radius:3px;overflow:hidden;margin-bottom:10px">
        <div style="width:{pct:.1f}%;height:100%;background:#3b82f6"></div></div>
      <table><tr><th>日期</th><th>大盘环境</th><th class="num">20日涨跌</th>
      <th>闸门</th><th class="num">主板候选</th></tr>{''.join(lrows)}</table>
      <div class="n" style="margin-top:8px;color:#6b7484">
      同一交易日重复运行只覆盖、不重复计数（幂等）。数据文件 data/stage1_log.json。</div>
    </div>
  </div>
"""


# ── 行情判断（量价四象限）──
# 统计值来自 regime_map.py（847 个交易日，2023-01 ~ 2026-09-18）
PANEL_STAT = {
    ("放量", "价涨"): (3.23, 76, "最该做", "good", "量价齐升——全场最好的一格"),
    ("放量", "价平"): (1.41, 62, "可以做", "good", "量在价先，资金已经进场"),
    ("放量", "价跌"): (1.61, 47, "小心做", "warn", "放量下跌多为恐慌盘，等止跌"),
    ("平量", "价涨"): (0.29, 53, "轻仓试", "warn", "力度不足，涨了也别重仓"),
    ("平量", "价平"): (0.59, 40, "观望", "warn", "没有方向，不做比乱做好"),
    ("平量", "价跌"): (2.53, 57, "可以低吸", "good", "缩量后的价跌，找超跌的"),
    ("缩量", "价涨"): (-1.52, 47, "不做", "bad", "无量上涨接不住，多为假突破"),
    ("缩量", "价平"): (-1.36, 36, "不做", "bad", "最差的一格，胜率只有 36%"),
    ("缩量", "价跌"): (2.67, 62, "准备做", "go", "超跌反弹，但要等放量确认"),
}
PANEL_COL = {"good": "#22a06b", "go": "#3b82f6", "warn": "#d9a441", "bad": "#f0544f"}


def do_regime():
    """调用 regime.py：算量价四象限 → data/regime.json"""
    try:
        import importlib
        import regime as R
        importlib.reload(R)
        R.main()
    except Exception as ex:
        print("  行情判断失败（不影响其他模块）：%s" % ex)


def svg_panel(cur_v, cur_p, w=560, h=300):
    """3×3 九宫格，高亮当前格"""
    order = [("放量", ["价涨", "价平", "价跌"]),
             ("平量", ["价涨", "价平", "价跌"]),
             ("缩量", ["价涨", "价平", "价跌"])]
    cw, ch, L, T = w / 3.0, (h - 40) / 3.0, 0, 30
    out = ['<svg viewBox="0 0 %d %d" width="100%%" height="%d">' % (w, h, h)]
    for j, lab in enumerate(["价涨", "价平", "价跌"]):
        out.append('<text x="%.1f" y="18" font-size="11" fill="#8b95a5" text-anchor="middle">%s</text>'
                   % (L + cw * j + cw / 2, lab))
    for i, (v, plist) in enumerate(order):
        for j, p in enumerate(plist):
            ret, win, act, col, _ = PANEL_STAT[(v, p)]
            x, y = L + cw * j, T + ch * i
            cur = (v == cur_v and p == cur_p)
            cc = PANEL_COL[col]
            fill = cc + ("44" if cur else "18")
            out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="6" fill="%s" '
                       'stroke="%s" stroke-width="%.1f"/>'
                       % (x + 3, y + 3, cw - 6, ch - 6, fill, cc, 2.5 if cur else 1))
            out.append('<text x="%.1f" y="%.1f" font-size="10.5" fill="#8b95a5">%s</text>'
                       % (x + 10, y + 17, v + p))
            out.append('<text x="%.1f" y="%.1f" font-size="14" font-weight="700" fill="%s" '
                       'text-anchor="end">%+.2f%%</text>'
                       % (x + cw - 8, y + 18, cc, ret))
            out.append('<text x="%.1f" y="%.1f" font-size="11.5" font-weight="700" fill="%s">%s</text>'
                       % (x + 10, y + 40, cc, act))
            out.append('<text x="%.1f" y="%.1f" font-size="10" fill="#6b7484">胜率 %d%%</text>'
                       % (x + 10, y + 56, win))
            if cur:
                out.append('<text x="%.1f" y="%.1f" font-size="10" font-weight="700" fill="%s">← 当前</text>'
                           % (x + cw - 8, y + 56, cc))
    out.append("</svg>")
    return "".join(out)


def svg_amount(rows, w=560, h=300):
    """近 20 日沪深成交额柱状图"""
    if not rows:
        return ""
    vals = [r["亿"] for r in rows]
    mx = max(vals) * 1.12
    avg = sum(vals) / len(vals)
    L, R, T, B = 8, 8, 22, 34
    pw, ph = w - L - R, h - T - B
    bw = pw / len(vals)
    out = ['<svg viewBox="0 0 %d %d" width="100%%" height="%d">' % (w, h, h)]
    out.append('<text x="8" y="14" font-size="11" fill="#8b95a5">沪深合计成交额（亿元）· 虚线＝前 20 日均值</text>')
    ya = T + ph - avg / mx * ph
    out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#3b82f6" stroke-width="1.4" '
               'stroke-dasharray="5 3" opacity="0.7"/>' % (L, ya, L + pw, ya))
    for i, r in enumerate(rows):
        v = r["亿"]
        x = L + bw * i
        hh = v / mx * ph
        col = "#f0544f" if v >= avg else "#5a6b80"
        out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="2" fill="%s" opacity="0.9"/>'
                   % (x + bw * 0.15, T + ph - hh, bw * 0.7, hh, col))
        if (i % 3 == 0 and i < len(rows) - 2) or i == len(rows) - 1:
            out.append('<text x="%.1f" y="%d" font-size="9" fill="#6b7484" text-anchor="middle">%s</text>'
                       % (x + bw / 2, h - 8, r["d"][5:]))
    out.append("</svg>")
    return "".join(out)


def render_regime():
    p = DATA / "regime.json"
    if not p.exists():
        return ""
    try:
        r = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return ""
    v, pd = r.get("量能档"), r.get("价格方向")
    st = PANEL_STAT.get((v, pd))
    if not st:
        return ""
    ret, win, act, colk, note = st
    col = PANEL_COL[colk]
    return """
  <div class="sec"><h2>今天该不该做（量价四象限 · %s）</h2>
    <div class="card">
      <div class="hero">
        <div>
          <div class="k">当前动作</div>
          <div class="emo" style="color:%s;font-size:44px">%s</div>
        </div>
        <div style="flex:1;min-width:280px;font-size:13.5px;color:#c6cedb">
          <div>量能：<b style="color:#e6e9ef">%s</b>（%+.1f%%，成交额 %.0f 亿 / 前20日均 %.0f 亿）</div>
          <div>价格：<b style="color:#e6e9ef">%s</b>（全市场等权近 20 日 %+.2f%%）</div>
          <div style="margin-top:6px">历史统计：之后 20 个交易日 <b style="color:%s">%+.2f%%</b>　·　
            胜率 <b style="color:%s">%d%%</b></div>
          <div style="margin-top:6px;color:#8b95a5">%s</div>
        </div>
      </div>
      <div style="display:flex;gap:14px;flex-wrap:wrap;margin-top:14px">
        <div style="flex:1;min-width:300px">%s</div>
        <div style="flex:1;min-width:300px">%s</div>
      </div>
    </div></div>
""" % (esc(r.get("日期", "")), col, esc(act), esc(r.get("量能档6", "")),
       r.get("量能变化%", 0), r.get("成交额亿", 0), r.get("前20日均亿", 0),
       esc(pd), r.get("等权20日涨跌%", 0), col, ret, col, win, esc(note),
       svg_panel(v, pd), svg_amount(r.get("近20日") or []))


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

    det = None
    tp = DATA / "today.json"
    if tp.exists():
        try:
            cand = json.loads(tp.read_text(encoding="utf-8"))
            if cand.get("date") == last["d"]:
                det = cand
        except Exception:
            det = None

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
  <div class="sub">数据日期 <b style="color:#e6e9ef">{esc(d)}</b>　·　数据来源：新浪财经行情（日线快照）＋ 通达信（指数成交额）
  　·　情绪值口径见《情绪周期与龙头实战》第 17 册，量价四象限见《什么行情用什么方法》　·　仅供参考，不构成投资建议</div>
{render_regime()}
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

  {render_swing()}

  {render_detail(det)}

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
        do_swing()
        do_regime()
    do_render()


if __name__ == "__main__":
    main()
