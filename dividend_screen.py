# -*- coding: utf-8 -*-
"""攒股收息 / 值得买的蓝筹股 —— 全市场分红与估值筛选

数据来源：
  ① 东财行情列表接口（多镜像兜底，全市场 5500+ 只）→ 现价/总市值/PE/PB/行业/股息率
  ② 东财分红送配接口（RPT_SHAREBONUS_DET）→ 逐只核验「已实施」现金分红、连续分红年数
  ③ 前复权日线（东财 → 腾讯 → 新浪，复用 swing_screen.fetch_one）→ MA20/MA60 位置

口径（写死在输出里，方便页面照实展示）：
  股息率(东财) = 东财 f133；股息率(近12月) = 除权日落在最近 12 个月内、且已实施的税前现金分红 ÷ 现价
  实收款一律按「税前」；两个口径取**较低值**用于筛选与排序（保守，避免被一次性/未实施分红误导）
  连续分红年数 = 从「最近一个应有年报的年份」往前数，年报（报告期 12-31）连续实施现金分红的年数
  ROE 估 = PB ÷ PE（由 市值/净利 与 市值/净资产 反推，非财报原始值，仅作横向比较）

输出：data/dividend.json
用法：python dividend_screen.py [--offline]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "127.0.0.1,localhost"

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
sys.path.insert(0, str(ROOT))
from swing_screen import fetch_one, fetch_names          # noqa: E402

OUT = DATA / "dividend.json"
REGIME = DATA / "regime.json"
HIST = DATA / "sentiment.json"
_TMP = Path(os.environ.get("TEMP", "/tmp"))
DIV_CACHE = _TMP / "div_kline_cache.json"        # 前复权日线（算 MA20/MA60）
SNAP_CACHE = _TMP / "div_snapshot_cache.json"    # 全市场快照（供 --offline 复用）
MET_CACHE = _TMP / "div_metrics_cache.json"      # 分红核验结果（供 --offline 复用）

# ── 行情列表接口：主机按序兜底（push2 主站偶发拒连） ──
LIST_HOSTS = ("push2test.eastmoney.com", "push2.eastmoney.com", "82.push2.eastmoney.com")
FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"      # 沪深 A 股（含创业板/科创板/北交所）
LIST_FIELDS = "f12,f14,f2,f9,f20,f21,f23,f100,f133"
UA_EM = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
UA_DC = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}

# ── 阈值（写进口径，页面照实展示） ──
DIV_MIN_YIELD = 3.5      # 收息榜：股息率(取低) ≥ 3.5%
DIV_MIN_MCAP = 200e8     #          总市值 ≥ 200 亿
DIV_MIN_YEARS = 3        #          连续分红年数 ≥ 3
BL_MIN_MCAP = 800e8      # 蓝筹榜：总市值 ≥ 800 亿
BL_MIN_YEARS = 5         #          连续分红年数 ≥ 5
BL_MIN_ROE = 8.0         #          ROE 估 ≥ 8%
BL_MIN_YIELD = 1.5       #          股息率(取低) ≥ 1.5%
PE_CAP = 25.0            # 收息榜 PE 上限
PB_CAP = 3.0             # 收息榜 PB 上限
BL_PE_CAP = 30.0         # 蓝筹榜 PE 上限
BL_PB_CAP = 5.0          # 蓝筹榜 PB 上限
TOP_N = 30               # 每个榜最多展示条数


def num(x, k):
    v = x.get(k)
    if v in (None, "-", ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def clean_name(nm: str) -> str:
    """去掉东财在名称前加的 XD/XR/DR（除息/除权/除权除息）标记"""
    nm = str(nm or "")
    for p in ("XD", "XR", "DR"):
        if nm.startswith(p) and len(nm) > len(p):
            return nm[len(p):]
    return nm


def fetch_list_page(pn, pz=500):
    """一页行情列表；逐个主机 + 重试"""
    err = None
    for host in LIST_HOSTS:
        u = ("https://%s/api/qt/clist/get?pn=%d&pz=%d&po=1&np=1&fltt=2&invt=2&fid=f20"
             "&fs=%s&fields=%s" % (host, pn, pz, FS, LIST_FIELDS))
        for attempt in range(2):
            try:
                raw = urllib.request.urlopen(
                    urllib.request.Request(u, headers=UA_EM),
                    timeout=30).read().decode("utf-8", "ignore")
                return (json.loads(raw).get("data") or {})
            except Exception as e:
                err = e
                time.sleep(0.8)
    print("  ⚠️ 第 %d 页拉取失败：%s" % (pn, type(err).__name__))
    return {}


def fetch_snapshot():
    """全市场快照（分页拼装）→ (rows, 请求到的最新价日期不可得, 总数)"""
    rows, total, t0 = [], None, time.time()
    for pn in range(1, 16):
        d = fetch_list_page(pn)
        rs = d.get("diff") or []
        if not rs:
            break
        total = d.get("total") or total
        rows += rs
        if total and len(rows) >= total:
            break
        time.sleep(0.5)
    print("  全市场快照 %d 只（total=%s，%.0fs）" % (len(rows), total, time.time() - t0))
    return rows


def fetch_div(code):
    """单只分红送配明细（按除权日倒序，最多 60 条）"""
    u = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
         "?reportName=RPT_SHAREBONUS_DET&columns=ALL"
         "&filter=(SECURITY_CODE%%3D%%22%s%%22)"
         "&sortColumns=EX_DIVIDEND_DATE&sortTypes=-1&pageSize=60&pageNumber=1"
         "&source=WEB&client=WEB" % code)
    for attempt in range(3):
        try:
            raw = urllib.request.urlopen(
                urllib.request.Request(u, headers=UA_DC),
                timeout=25).read().decode("utf-8", "ignore")
            return ((json.loads(raw).get("result") or {}).get("data") or [])
        except Exception:
            time.sleep(0.7)
    return None                                  # None = 抓取失败（区别于「无分红记录」[]）


def fetch_sec_name(code):
    """取该股完整简称（除权除息日名称会被加上 XD/XR/DR 前缀并被行情接口截断）。
    东财分红明细接口的 SECURITY_NAME_ABBR 是干净全称，例如 600941 → 中国移动。"""
    u = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
         "?reportName=RPT_SHAREBONUS_DET&columns=SECURITY_CODE,SECURITY_NAME_ABBR"
         "&filter=(SECURITY_CODE%%3D%%22%s%%22)&pageSize=1&pageNumber=1"
         "&source=WEB&client=WEB" % code)
    try:
        raw = urllib.request.urlopen(
            urllib.request.Request(u, headers=UA_DC),
            timeout=20).read().decode("utf-8", "ignore")
        d = ((json.loads(raw).get("result") or {}).get("data") or [])
        nm = (d[0].get("SECURITY_NAME_ABBR") or "").strip() if d else ""
        return code, nm
    except Exception:
        return code, ""


def div_metrics(recs, ref_date, price):
    """由分红明细算：近12月已实施每股分红 / 连续分红年数 / 近3年年报每股分红"""
    try:
        ref = datetime.strptime(ref_date, "%Y-%m-%d")
    except Exception:
        ref = datetime.now()
    lo = ref - timedelta(days=365)

    dps12, ev12 = 0.0, 0
    seasons = {}
    for r in recs:
        st = str(r.get("ASSIGN_PROGRESS") or "")
        if "实施" not in st:
            continue
        exd = str(r.get("EX_DIVIDEND_DATE") or "")[:10]
        amt = num(r, "PRETAX_BONUS_RMB")
        if amt is None or amt <= 0:
            continue
        # 近 12 个月已实施（除权日已过且在窗口内）
        if len(exd) == 10:
            try:
                d = datetime.strptime(exd, "%Y-%m-%d")
                if lo < d <= ref:
                    dps12 += amt / 10.0
                    ev12 += 1
            except ValueError:
                pass
        # 年报（报告期 12-31）口径 → 连续分红年数
        rd = str(r.get("REPORT_DATE") or "")[:10]
        if len(rd) == 10 and rd[5:7] == "12":
            try:
                y = int(rd[:4])
            except ValueError:
                continue
            seasons[y] = seasons.get(y, 0.0) + amt / 10.0

    # 最近一个「应有年报」的年份：5 月后应已有上年年报，5 月前只能算前年
    latest_expected = ref.year - 1 if ref.month >= 5 else ref.year - 2
    n, y = 0, latest_expected
    while y in seasons:
        n += 1
        y -= 1
    hist3 = [round(seasons[k], 4) for k in
             (latest_expected, latest_expected - 1, latest_expected - 2) if k in seasons]

    dy_self = (dps12 / price * 100.0) if (price and dps12 > 0) else 0.0
    return {"近12月每股分红": round(dps12, 4), "近12月分红次数": ev12,
            "股息率近12月": round(dy_self, 2),
            "连续分红年数": n, "近3年年报每股分红": hist3}


def load_klines(codes, ref_date, offline=False):
    """前复权日线（仅用于算 MA20/MA60），独立缓存，只缓存不写坏"""
    data = {}
    if DIV_CACHE.exists():
        try:
            data = json.loads(DIV_CACHE.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    def stale(c):
        b = data.get(c)
        if not b or len(b) < 60:
            return True
        return bool(ref_date) and b[-1][0] < ref_date

    need = [c for c in codes if stale(c)]
    if offline or not need:
        print("  日线：用缓存 %d 只（需新拉 %d 只）" % (len(data), len(need)))
        return data
    print("  日线：拉取 %d 只（东财 → 腾讯 → 新浪）…" % len(need))
    t0, got = time.time(), 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        for f in as_completed([ex.submit(fetch_one, c) for c in need]):
            code, _nm, bars = f.result()
            if bars:
                data[code] = bars
                got += 1
    if got:
        try:
            DIV_CACHE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            print("  日线缓存写入失败（不影响本次）：%s" % e)
    else:
        print("  ⚠️ 日线全部拉取失败，保留旧缓存")
    print("  日线完成 %d 只（%.0fs）" % (got, time.time() - t0))
    return data


def ma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="行情用缓存/不联网")
    a = ap.parse_args()

    # ── 参考交易日 ──
    ref = ""
    for f in (REGIME, HIST):
        if f.exists():
            try:
                j = json.loads(f.read_text(encoding="utf-8"))
                ref = max(ref, str(j.get("日期") or j.get("updated") or ""))
            except Exception:
                pass
    if not ref:
        raise SystemExit("缺 data/regime.json / data/sentiment.json，无法确定交易日")

    # ── ① 全市场快照 ──
    if a.offline and SNAP_CACHE.exists():
        try:
            rows = json.loads(SNAP_CACHE.read_text(encoding="utf-8"))
            print("离线模式：快照用缓存 %d 只" % len(rows))
        except Exception:
            rows = []
    else:
        rows = fetch_snapshot()
        if rows:
            try:
                SNAP_CACHE.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            except Exception as e:
                print("  快照缓存写入失败（不影响本次）：%s" % e)
    if not rows:
        raise SystemExit("行情快照为空，终止（不覆盖已有结果）")

    base, need_name = [], []
    for x in rows:
        code = str(x.get("f12") or "")
        raw = str(x.get("f14") or "")
        nm = clean_name(raw)
        if not code or not nm:
            continue
        if "ST" in nm.upper() or "退" in nm:
            continue
        price = num(x, "f2")
        mcap = num(x, "f20")
        pe, pb = num(x, "f9"), num(x, "f23")
        dy_em = num(x, "f133")
        if not price or price <= 0 or not mcap:
            continue
        # 东财列表接口对带 XD/XR/DR 前缀的名称会截断（如「XD中国移」），
        # 这类标记为待补全，稍后用新浪行情接口取回完整名称
        if raw[:2] in ("XD", "XR", "DR") and len(raw) <= 5:
            need_name.append(code)
        base.append({"code": code, "name": nm, "ind": str(x.get("f100") or ""),
                     "price": price, "mcap": mcap, "pe": pe, "pb": pb,
                     "dy_em": dy_em or 0.0})

    if need_name:
        ok = set()
        with ThreadPoolExecutor(max_workers=6) as ex:
            for f in as_completed([ex.submit(fetch_sec_name, c) for c in need_name]):
                c, full = f.result()
                if not full:
                    continue
                for b in base:
                    if b["code"] == c:
                        b["name"] = full
                        ok.add(c)
        rest = [c for c in need_name if c not in ok]
        if rest:                                   # 兜底：新浪行情（可能仍带 XD 前缀且被截断）
            alt = fetch_names(rest)
            for b in base:
                if b["code"] in alt and alt[b["code"]]:
                    b["name"] = clean_name(alt[b["code"]])
                    ok.add(b["code"])
        print("  补全「XD/XR/DR」被截断的名称 %d/%d 只" % (len(ok), len(need_name)))

    n_y3 = sum(1 for b in base if b["dy_em"] >= 3.0)
    n_y5 = sum(1 for b in base if b["dy_em"] >= 5.0)

    # ── ② 预筛（分红核验前的粗筛） ──
    pre_div, pre_blue = [], []
    for b in base:
        pe, pb = b["pe"], b["pb"]
        if (b["dy_em"] >= DIV_MIN_YIELD and b["mcap"] >= DIV_MIN_MCAP
                and pe and 0 < pe <= PE_CAP and pb and 0 < pb <= PB_CAP):
            pre_div.append(b)
        if (b["mcap"] >= BL_MIN_MCAP and pe and 0 < pe <= BL_PE_CAP
                and pb and 0 < pb <= BL_PB_CAP and b["dy_em"] >= BL_MIN_YIELD):
            pre_blue.append(b)
    pre_div.sort(key=lambda z: -z["dy_em"])
    print("预筛：收息 %d 只｜蓝筹 %d 只" % (len(pre_div), len(pre_blue)))

    uniq = {b["code"]: b for b in pre_div + pre_blue}
    codes = list(uniq)
    divmap, failed = {}, []
    if a.offline:
        try:
            divmap, failed = json.loads(MET_CACHE.read_text(encoding="utf-8"))
            print("离线模式：分红明细用缓存 %d 只" % len(divmap))
        except Exception:
            divmap, failed = {}, []
    if not a.offline or not divmap:
        print("分红核验 %d 只（东财分红送配接口）…" % len(codes))
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(fetch_div, c): c for c in codes}
            for f in as_completed(futs):
                c = futs[f]
                try:
                    r = f.result()
                except Exception:
                    r = None
                if r is None:
                    failed.append(c)
                    continue
                b = uniq[c]
                divmap[c] = div_metrics(r, ref, b["price"])
        print("  完成 %d 只（失败 %d，%.0fs）" % (len(divmap), len(failed), time.time() - t0))
        if divmap:
            try:
                MET_CACHE.write_text(json.dumps([divmap, failed], ensure_ascii=False),
                                     encoding="utf-8")
            except Exception as e:
                print("  分红明细缓存写入失败（不影响本次）：%s" % e)

    # ── 组装：两个榜 ──
    def enrich(b):
        m = divmap.get(b["code"]) or {"近12月每股分红": None, "近12月分红次数": 0,
                                      "股息率近12月": None, "连续分红年数": None,
                                      "近3年年报每股分红": []}
        dy_self = m.get("股息率近12月")
        dy_low = min(b["dy_em"], dy_self) if dy_self not in (None, 0.0) else b["dy_em"]
        roe = (b["pb"] / b["pe"] * 100.0) if (b["pe"] and b["pb"]) else None
        return {
            "代码": b["code"], "名称": b["name"], "行业": b["ind"],
            "现价": round(b["price"], 2), "总市值亿": round(b["mcap"] / 1e8),
            "PE": round(b["pe"], 1) if b["pe"] else None,
            "PB": round(b["pb"], 2) if b["pb"] else None,
            "ROE估": round(roe, 1) if roe is not None else None,
            "股息率东财": round(b["dy_em"], 2),
            "股息率近12月": dy_self,
            "股息率取低": round(dy_low, 2),
            "每万元年分红": round(dy_low * 100),      # 1 万元 × 股息率
            "连续分红年数": m.get("连续分红年数"),
            "近3年年报每股分红": m.get("近3年年报每股分红"),
            "分红数据缺失": b["code"] in failed,
        }

    div_rows = [enrich(b) for b in pre_div if b["code"] in divmap]
    blue_rows = [enrich(b) for b in pre_blue if b["code"] in divmap]

    div_rows = [r for r in div_rows
                if (r["连续分红年数"] or 0) >= DIV_MIN_YEARS and r["股息率取低"] >= DIV_MIN_YIELD]
    div_rows.sort(key=lambda r: -r["股息率取低"])

    def blue_score(r):
        dy = min((r["股息率取低"] or 0) / 6.0, 1.0)
        q = min((r["ROE估"] or 0) / 20.0, 1.0)
        v = max(0.0, 1.0 - (r["PE"] or 40) / 35.0)
        return round(dy * 50 + q * 30 + v * 20, 1)

    blue_rows = [r for r in blue_rows
                 if (r["连续分红年数"] or 0) >= BL_MIN_YEARS
                 and (r["ROE估"] or 0) >= BL_MIN_ROE and r["股息率取低"] >= BL_MIN_YIELD]
    for r in blue_rows:
        r["综合分"] = blue_score(r)
    blue_rows.sort(key=lambda r: (-r["综合分"], -r["股息率取低"]))

    # ── ③ 日线 → MA20 / MA60 / 位置 ──
    picks = {r["代码"] for r in div_rows[:TOP_N] + blue_rows[:TOP_N]}
    bars_map = load_klines(sorted(picks), ref, offline=a.offline)

    def attach(r):
        bars = bars_map.get(r["代码"])
        if not bars or len(bars) < 60:
            r.update({"MA20": None, "MA60": None, "距MA60%": None, "近20日%": None,
                      "位置": "无日线", "动作": "—"})
            return r
        cl = [b[4] for b in bars]
        m20, m60 = ma(cl, 20), ma(cl, 60)
        last = cl[-1]
        r["MA20"] = round(m20, 2) if m20 else None
        r["MA60"] = round(m60, 2) if m60 else None
        r["距MA60%"] = round((last / m60 - 1) * 100, 2) if m60 else None
        r["近20日%"] = round((last / cl[-21] - 1) * 100, 2) if len(cl) > 21 else None
        if m60 and last >= m60:
            r["位置"] = "站上 MA60"
            r["动作"] = "可分批建仓"
        elif m60 and last >= m60 * 0.95:
            r["位置"] = "贴近 MA60"
            r["动作"] = "等站上 MA60"
        else:
            r["位置"] = "跌破 MA60"
            r["动作"] = "先等，别接刀"
        return r

    div_rows = [attach(r) for r in div_rows[:TOP_N]]
    blue_rows = [attach(r) for r in blue_rows[:TOP_N]]

    miss_diag = len([r for r in div_rows + blue_rows if r["位置"] == "无日线"])

    out = {
        "日期": ref,
        "全市场": {"股票数": len(base), "股息率东财≥3%": n_y3, "股息率东财≥5%": n_y5},
        "收息榜": div_rows,
        "蓝筹榜": blue_rows,
        "阈值": {
            "收息": "股息率(取低)≥%.1f%%｜总市值≥%.0f亿｜PE≤%.0f｜PB≤%.1f｜连续分红≥%d年"
                    % (DIV_MIN_YIELD, DIV_MIN_MCAP / 1e8, PE_CAP, PB_CAP, DIV_MIN_YEARS),
            "蓝筹": "总市值≥%.0f亿｜PE≤%.0f｜PB≤%.1f｜ROE估≥%.0f%%｜股息率(取低)≥%.1f%%｜连续分红≥%d年"
                    % (BL_MIN_MCAP / 1e8, BL_PE_CAP, BL_PB_CAP, BL_MIN_ROE,
                       BL_MIN_YIELD, BL_MIN_YEARS),
        },
        "口径": [
            "股息率(东财) = 东财行情接口 f133；股息率(近12月) = 除权日落在最近 12 个月内且已实施的税前现金分红 ÷ 现价",
            "筛选与排序一律用两者中的**较低值**（保守口径），避免被一次性分红或「已预案未除权」的分红抬高",
            "连续分红年数 = 从最近一个应有年报的年份往前数，年报（报告期 12-31）连续「实施分配」的年数",
            "ROE 估 = PB ÷ PE（由 市值/净资产 与 市值/净利 反推，非财报原始值，仅供横向比较）",
            "分红数据仅统计「实施分配」记录，已预案未除权的不计入",
        ],
        "说明": ("攒股收息是「买了长期拿分红」的思路：不择时、分批买、越跌越买"
                 "（分红不变时价格跌 = 股息率升）；唯一的卖出理由是「连续分红中断」。"
                 "蓝筹榜是「便宜的好公司」清单，但要不要动手还要看位置——"
                 "站上 MA60 才算右侧，跌破 MA60 就先等。"),
        "免责": "公开数据筛选结果，不是推荐，也不构成投资建议；历史分红不保证未来继续分红。",
        "数据源": "东财行情列表接口 ＋ 东财分红送配接口（RPT_SHAREBONUS_DET） ＋ 前复权日线（东财/腾讯/新浪）",
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n数据日期 %s" % ref)
    print("全市场 %d 只｜股息率(东财)≥3%% 有 %d 只、≥5%% 有 %d 只" % (len(base), n_y3, n_y5))
    print("收息榜 %d 只：" % len(div_rows))
    for r in div_rows[:10]:
        print("  %s %-8s %-6s 股息%.2f%%(东财%.2f%%) 万元年分红%.0f元 PE%.1f PB%.2f 连续%d年 %s"
              % (r["代码"], r["名称"][:8], r["行业"][:6], r["股息率取低"], r["股息率东财"],
                 r["每万元年分红"], r["PE"] or 0, r["PB"] or 0, r["连续分红年数"] or 0, r["动作"]))
    print("蓝筹榜 %d 只：" % len(blue_rows))
    for r in blue_rows[:10]:
        print("  %s %-8s %-6s 分%.0f 市值%.0f亿 股息%.2f%% ROE%.1f%% PE%.1f %s"
              % (r["代码"], r["名称"][:8], r["行业"][:6], r["综合分"], r["总市值亿"],
                 r["股息率取低"], r["ROE估"] or 0, r["PE"] or 0, r["动作"]))
    if miss_diag:
        print("  ⚠️ 有 %d 只拿不到日线，位置列显示「无日线」" % miss_diag)
    if failed:
        print("  ⚠️ 分红明细抓取失败 %d 只（未纳入榜单）" % len(failed))
    print("已写 %s" % OUT)


if __name__ == "__main__":
    main()
