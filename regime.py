# -*- coding: utf-8 -*-
"""行情判断（量价四象限）→ 今天该不该做

口径（与《什么行情用什么方法》手册一致）
  量能  = 当日沪深合计成交额 ÷ 前 20 日均值
  价格  = 看板等权指数近 20 日涨跌（用 sentiment.json 的 eq 累积）
  九宫格统计值来自 regime_map.py（样本 847 个交易日，2023-01 ~ 2026-09-18）

数据源
  历史成交额：通达信上证指数/深证成指日线成交额（种子）
  每日追加：新浪实时接口 hq.sinajs.cn（第 9 个字段＝成交额，单位元）
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
HIST = DATA / "regime_history.json"
HIST_S = DATA / "sentiment.json"
OUT = DATA / "regime.json"

# ── 种子：沪深合计成交额（亿元），来自通达信上证指数 + 深证成指日线 Amount ──
# 注意：只放「沪深两市合计」的数据（7-29 起）；更早的只有沪市单市，会污染均值，不入库
SEED = {
    "2026-07-29": 22965.8, "2026-07-30": 23428.1, "2026-07-31": 25419.5,
    "2026-08-03": 19974.2, "2026-08-04": 22135.9, "2026-08-05": 26596.3,
    "2026-08-06": 25287.8, "2026-08-07": 26644.2, "2026-08-10": 25231.0,
    "2026-08-11": 23209.9, "2026-08-12": 21529.2, "2026-08-13": 25509.2,
    "2026-08-14": 21428.4, "2026-08-17": 23874.6, "2026-08-18": 24007.8,
    "2026-08-19": 25110.4, "2026-08-20": 20793.6, "2026-08-21": 18792.6,
    "2026-08-24": 20074.6, "2026-08-25": 18318.4, "2026-08-26": 18087.2,
    "2026-08-27": 21259.3, "2026-08-28": 21017.1, "2026-08-31": 21310.3,
    "2026-09-01": 20340.0, "2026-09-02": 17911.8, "2026-09-03": 17589.1,
    "2026-09-04": 20306.7, "2026-09-07": 19460.1, "2026-09-08": 19603.4,
    "2026-09-09": 18556.0, "2026-09-10": 16471.5, "2026-09-11": 19719.0,
    "2026-09-14": 16291.7, "2026-09-15": 16127.1, "2026-09-16": 18391.2,
    "2026-09-17": 18231.3, "2026-09-18": 20771.0, "2026-09-21": 20315.1,
    "2026-09-22": 21355.5, "2026-09-23": 17649.7, "2026-09-24": 16533.5,
    "2026-09-28": 17028.0, "2026-09-29": 14091.9, "2026-09-30": 14379.9,
}

# ── 九宫格：量能 × 价格 → 该怎么做 ──
# 统计值来自 regime_map.py（847 个交易日）
PANEL = {
    ("放量", "价涨"): {"ret": 3.23, "win": 76, "act": "最该做", "col": "good",
                    "note": "量价齐升——全场最好的一格"},
    ("放量", "价平"): {"ret": 1.41, "win": 62, "act": "可以做", "col": "good",
                    "note": "量在价先，资金已经进场"},
    ("放量", "价跌"): {"ret": 1.61, "win": 47, "act": "小心做", "col": "warn",
                    "note": "放量下跌多为恐慌盘，等止跌"},
    ("平量", "价涨"): {"ret": 0.29, "win": 53, "act": "轻仓试", "col": "warn",
                    "note": "力度不足，涨了也别重仓"},
    ("平量", "价平"): {"ret": 0.59, "win": 40, "act": "观望", "col": "warn",
                    "note": "没有方向，不做比乱做好"},
    ("平量", "价跌"): {"ret": 2.53, "win": 57, "act": "可以低吸", "col": "good",
                    "note": "缩量后的价跌，找超跌的"},
    ("缩量", "价涨"): {"ret": -1.52, "win": 47, "act": "不做", "col": "bad",
                    "note": "无量上涨接不住，多为假突破"},
    ("缩量", "价平"): {"ret": -1.36, "win": 36, "act": "不做", "col": "bad",
                    "note": "最差的一格，胜率只有 36%"},
    ("缩量", "价跌"): {"ret": 2.67, "win": 62, "act": "准备做", "col": "go",
                    "note": "超跌反弹，但要等放量确认"},
}


def fetch_now_amount():
    """新浪实时：返回 (日期, 沪深合计成交额亿元)"""
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ.pop(k, None)
    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
    url = "http://hq.sinajs.cn/list=sh000001,sz399001"
    req = urllib.request.Request(
        url, headers={"Referer": "https://finance.sina.com.cn",
                      "User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=15).read().decode("gbk", "ignore")
    tot, date = 0.0, ""
    for line in raw.strip().split("\n"):
        if "hq_str_" not in line:
            continue
        vals = line.split('"')[1].split(",")
        if len(vals) < 10:
            continue
        try:
            tot += float(vals[9])
        except (ValueError, IndexError):
            continue
        if not date and len(vals) > 30:
            date = vals[30]
    if tot <= 0:
        return None, None
    return date, tot / 1e8


def band_of(ratio):
    """量能档位（九宫格口径）"""
    if ratio < -5:
        return "缩量"
    if ratio > 10:
        return "放量"
    return "平量"


def band6(ratio):
    if ratio < -25:
        return "极度缩量"
    if ratio < -15:
        return "明显缩量"
    if ratio < -5:
        return "温和缩量"
    if ratio < 10:
        return "量能平稳"
    if ratio < 25:
        return "温和放量"
    return "明显放量"


def price_dir(hist):
    """用等权指数近 20 日涨跌定价格方向"""
    if not HIST_S.exists():
        return None, None
    d = json.loads(HIST_S.read_text(encoding="utf-8"))
    lv, dt = 1.0, []
    for r in d["daily"]:
        e = r.get("eq")
        if e is None:
            continue
        lv *= (1 + e / 100.0)
        dt.append(lv)
    if len(dt) < 21:
        return None, None
    chg = (dt[-1] / dt[-21] - 1) * 100
    if chg > 3:
        return "价涨", chg
    if chg < -3:
        return "价跌", chg
    return "价平", chg


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    DATA.mkdir(exist_ok=True)

    hist = {}
    if HIST.exists():
        hist = json.loads(HIST.read_text(encoding="utf-8")).get("amounts", {})
    added = 0
    for k, v in SEED.items():
        if k not in hist:
            hist[k] = v
            added += 1
    print("种子历史：%d 天（新增 %d）" % (len(hist), added))

    # 当日
    d, amt = None, None
    try:
        d, amt = fetch_now_amount()
    except Exception as e:
        print("  实时成交额获取失败：%s" % e)
    if d and amt:
        hist[d] = round(amt, 1)
        print("  最新 %s：沪深合计 %.0f 亿" % (d, amt))

    HIST.write_text(json.dumps({"amounts": hist}, ensure_ascii=False), encoding="utf-8")
    ds = sorted(hist)
    last = ds[-1]
    if len(ds) < 21:
        print("历史不足 21 天，无法计算前 20 日均值")
        return
    base = sum(hist[x] for x in ds[-21:-1]) / 20.0        # 前 20 日（不含当日）
    ratio = (hist[last] / base - 1) * 100
    vb, v6 = band_of(ratio), band6(ratio)
    pdir, pchg = price_dir(hist)
    if pdir is None:
        print("无法判定价格方向（sentiment.json 不足）")
        return
    p = PANEL.get((vb, pdir), {})

    out = {
        "日期": last,
        "成交额亿": hist[last],
        "前20日均亿": round(base, 1),
        "量能变化%": round(ratio, 1),
        "量能档": vb,
        "量能档6": v6,
        "价格方向": pdir,
        "等权20日涨跌%": round(pchg, 2),
        "动作": p.get("act", "-"),
        "色": p.get("col", "warn"),
        "后20日%": p.get("ret"),
        "胜率%": p.get("win"),
        "说明": p.get("note", ""),
        "历史天数": len(ds),
        "近20日": [{"d": x, "亿": hist[x]} for x in ds[-20:]],
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n%s｜量能 %s（%+.1f%%）｜价格 %s（等权20日 %+.2f%%）"
          % (last, v6, ratio, pdir, pchg))
    print("→ %s：%s（历史后20日 %+.2f%%、胜率 %d%%）"
          % (out["动作"], out["说明"], p["ret"], p["win"]))


if __name__ == "__main__":
    main()
