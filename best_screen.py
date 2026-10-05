# -*- coding: utf-8 -*-
"""最优策略信号：ETF · KDJ 低位金叉（K 上穿 D 且 K<40）· 收盘跌破 MA20 退出 · 老闸门 ±3%

输出 data/best.json
用法：python best_screen.py [--offline]
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
sys.path.insert(0, str(ROOT))
from swing_screen import fetch_one                      # noqa: E402

BEST_CACHE = Path(os.environ.get("TEMP", "/tmp")) / "best_kline_cache.json"
ETF_CODES = DATA / "etf_codes.json"
REGIME = DATA / "regime.json"
POSITIONS = DATA / "best_positions.json"
OUT = DATA / "best.json"

K_HI = 40.0          # 「低位」阈值
GATE = 3.0           # 闸门 ±3%

# 兜底过滤：名称里含这些的一律不要（货币/债券/商品）
EXCLUDE = ("货币", "快线", "快钱", "日利", "添益", "保证金", "现金", "债", "国债", "政金",
           "城投", "短融", "地方债", "国开", "转债", "黄金", "白银", "豆粕", "原油",
           "有色", "能源化工", "商品", "标普", "纳指", "纳斯达克", "恒生", "港股",
           "中概", "日经", "德国", "法国", "美国", "海外", "全球")


def kdj_series(bars, n=9):
    """返回 (K[], D[]) —— 与 allstrat.analyze 同口径"""
    K = D = 50.0
    ks, ds = [], []
    for i in range(len(bars)):
        if i < n - 1:
            ks.append(None)
            ds.append(None)
            continue
        hh = max(b[2] for b in bars[i - n + 1:i + 1])
        ll = min(b[3] for b in bars[i - n + 1:i + 1])
        rsv = (bars[i][4] - ll) / (hh - ll) * 100 if hh > ll else 50.0
        K = 2 / 3 * K + 1 / 3 * rsv
        D = 2 / 3 * D + 1 / 3 * K
        ks.append(K)
        ds.append(D)
    return ks, ds


def ma_s(vals, n, i):
    if i + 1 < n:
        return None
    return sum(vals[i - n + 1:i + 1]) / n


def load_bars(codes, offline=False):
    data = {}
    if BEST_CACHE.exists():
        try:
            data = json.loads(BEST_CACHE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    # 参考日期：交易日历以 regime.json / sentiment.json 的最新日期为准
    ref = ""
    for f in (REGIME, DATA / "sentiment.json"):
        if f.exists():
            try:
                j = json.loads(f.read_text(encoding="utf-8"))
                ref = max(ref, str(j.get("日期") or j.get("updated") or ""))
            except Exception:
                pass

    def stale(c):
        b = data.get(c)
        if not b or len(b) < 90:
            return True
        return bool(ref) and b[-1][0] < ref          # 缓存落后于最新交易日 → 需要刷新

    need = [c for c in codes if stale(c)]
    if offline or not need:
        if offline:
            print("离线模式：使用缓存（%d 只）" % len(data))
        else:
            print("行情缓存已是最新（%d 只，截至 %s）" % (len(data), ref or "—"))
        return data
    print("拉取 %d 只（东财前复权 → 腾讯 → 新浪）…" % len(need))
    t0 = time.time()
    got = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        for f in as_completed([ex.submit(fetch_one, c) for c in need]):
            code, _nm, bars = f.result()
            if bars:
                data[code] = bars
                got += 1
    if got:                                   # 只在拿到数据时才写缓存（避免写坏）
        try:
            BEST_CACHE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            print("  缓存写入失败（不影响本次）：%s" % e)
    else:
        print("  ⚠️ 全部拉取失败，保留旧缓存")
    print("  完成 %d 只（%.0fs）" % (got, time.time() - t0))
    return data


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="只用缓存，不联网")
    a = ap.parse_args()

    if not ETF_CODES.exists():
        print("缺 data/etf_codes.json")
        return
    pool = [(c, n) for c, n in json.loads(ETF_CODES.read_text(encoding="utf-8"))
            if not any(k in n for k in EXCLUDE)]
    print("ETF 池 %d 只（已剔除货币/债券/商品/跨境）" % len(pool))

    bars_map = load_bars([c for c, _ in pool], offline=a.offline)

    # 闸门
    gate_val, gate_open, gate_txt = None, None, "数据不足"
    if REGIME.exists():
        rg = json.loads(REGIME.read_text(encoding="utf-8"))
        gate_val = rg.get("等权20日涨跌%")
        if gate_val is not None:
            gate_open = abs(gate_val) > GATE
            gate_txt = ("开放（近 20 日 %+.2f%%，超出 ±3%%）" % gate_val if gate_open
                        else "关闭（近 20 日 %+.2f%%，在 ±3%% 内 → 今天不开新仓）" % gate_val)

    sigs, watch, base_date = [], [], ""
    for code, nm in pool:
        bars = bars_map.get(code)
        if not bars or len(bars) < 30:
            continue
        base_date = max(base_date, bars[-1][0])
        ks, ds = kdj_series(bars)
        i = len(bars) - 1
        if ks[i] is None or ks[i - 1] is None:
            continue
        cl = bars[i][4]
        m20 = ma_s([b[4] for b in bars], 20, i)
        amt20 = None
        if len(bars[0]) > 6:
            amt20 = sum(b[6] for b in bars[i - 19:i + 1]) / 20 if i >= 19 else None
        rec = {"代码": code, "名称": nm, "收盘": round(cl, 4),
               "K": round(ks[i], 1), "D": round(ds[i], 1),
               "MA20": round(m20, 4) if m20 else None,
               "高于MA20%": round((cl / m20 - 1) * 100, 2) if m20 else None,
               "20日均额亿": round(amt20 / 1e8, 2) if amt20 else None}
        if ks[i] > ds[i] and ks[i - 1] <= ds[i - 1] and ks[i] < K_HI:
            rec["信号"] = "K 上穿 D（金叉），K=%.1f < 40" % ks[i]
            sigs.append(rec)
        elif ks[i] < K_HI and ks[i] <= ds[i]:
            rec["信号"] = "K=%.1f < 40，等金叉" % ks[i]
            watch.append(rec)

    sigs.sort(key=lambda x: -x["K"])
    watch.sort(key=lambda x: x["K"])

    # 持仓监控
    holds = []
    if POSITIONS.exists():
        try:
            raw = json.loads(POSITIONS.read_text(encoding="utf-8"))
            ps = raw.get("positions", []) if isinstance(raw, dict) else raw
        except Exception:
            ps = []
        for p in ps:
            code = str(p.get("code") or p.get("代码") or "")
            bars = bars_map.get(code)
            if not bars:
                holds.append({"代码": code, "名称": p.get("name", code), "状态": "无行情"})
                continue
            cl = bars[-1][4]
            m20 = ma_s([b[4] for b in bars], 20, len(bars) - 1)
            bp = p.get("buy_price") or p.get("买价")
            holds.append({
                "代码": code, "名称": p.get("name") or p.get("名称") or code,
                "买价": bp, "现价": round(cl, 4),
                "持有收益%": round((cl / bp - 1) * 100, 2) if bp else None,
                "MA20": round(m20, 4) if m20 else None,
                "距MA20%": round((cl / m20 - 1) * 100, 2) if m20 else None,
                "动作": ("⚠️ 已跌破 MA20 → 次日开盘卖" if (m20 and cl < m20) else "持有（未跌破 MA20）"),
            })

    out = {
        "日期": base_date,
        "池子大小": len(pool),
        "有行情": len(bars_map),
        "闸门值": gate_val, "闸门开放": gate_open, "闸门说明": gate_txt,
        "今日信号数": len(sigs), "今日信号": sigs[:10],
        "观察池": watch[:10], "观察池总数": len(watch),
        "持仓": holds,
        "口径": "ETF（行业主题+宽基）× KDJ低位金叉（K上穿D 且 K<40）× 收盘跌破MA20退出 × 老闸门±3%；"
                "单笔 6,000 元、最多 2 只；历史实测 3.28 年 +43.82%、回撤 −12.74%、盈亏比 2.46",
        "免责": "历史形态筛选结果，不构成投资建议",
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\n数据日期 %s" % base_date)
    print("闸门：%s" % gate_txt)
    print("今日信号 %d 只：" % len(sigs))
    for r in sigs[:8]:
        print("  %s %-14s 收 %.3f  K=%.1f 高于MA20 %+.2f%%" %
              (r["代码"], r["名称"], r["收盘"], r["K"], r["高于MA20%"] or 0))
    if not sigs:
        print("  （无）")
    print("观察池（K<40 待金叉）%d 只，K 最低的 5 只：" % len(watch))
    for r in watch[:5]:
        print("  %s %-14s K=%.1f D=%.1f 高于MA20 %+.2f%%" %
              (r["代码"], r["名称"], r["K"], r["D"], r["高于MA20%"] or 0))
    print("\n已写 %s" % OUT)


if __name__ == "__main__":
    main()
