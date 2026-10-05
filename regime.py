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

# ── 每个状态下「正期望」的策略（来源：regime_signal_study.py，105 万个信号）──
# 格式：策略名 平均单笔收益%/胜率%
STRAT_BY_REGIME = {
    "放量+价涨": {"n": 22, "list": [
        "ETF S02 均线金叉MA5×MA20　+6.22% / 72.6%",
        "ETF S14 MACD零轴下金叉　+5.69% / 70.2%",
        "个股 S02 均线金叉MA5×MA20　+4.64% / 60.2%",
        "ETF S10 箱体突破　+4.43% / 71.3%",
        "ETF S11 BOLL收口突破　+3.99% / 70.0%"]},
    "放量+价平": {"n": 18, "list": [
        "个股 S13 KDJ低位金叉　+5.35% / 61.2%",
        "个股 S06 三倍量+低位　+4.14% / 60.1%",
        "ETF S06 三倍量+低位　+4.08% / 55.0%",
        "个股 S14 MACD零轴下金叉　+3.76% / 57.4%",
        "个股 S11 BOLL收口突破　+3.59% / 52.2%"]},
    "放量+价跌": {"n": 14, "list": [
        "个股 S05 三倍量+前3日下跌　+10.86% / 58.9%",
        "个股 S06 三倍量+低位　+8.26% / 60.3%",
        "个股 S13 KDJ低位金叉　+6.90% / 63.7%",
        "ETF S06 三倍量+低位　+6.16% / 79.9%",
        "ETF S05 三倍量+前3日下跌　+5.95% / 76.3%"]},
    "平量+价涨": {"n": 4, "list": [
        "ETF S10 箱体突破　+3.38% / 71.8%",
        "ETF S07 突破60日新高　+2.29% / 65.3%",
        "个股 S10 箱体突破　+1.27% / 45.7%",
        "ETF S08 放量突破前20日高　+1.10% / 51.7%"]},
    "平量+价平": {"n": 13, "list": [
        "ETF S11 BOLL收口突破　+6.81% / 79.5%",
        "个股 S11 BOLL收口突破　+4.01% / 56.5%",
        "个股 S09 缩量回调不破MA20　+3.21% / 55.4%",
        "ETF S14 MACD零轴下金叉　+2.81% / 56.4%"]},
    "平量+价跌": {"n": 15, "list": [
        "ETF S13 KDJ低位金叉　+4.24% / 61.0%",
        "ETF S06 三倍量+低位　+3.90% / 62.7%",
        "ETF S03 周线KDJ凹底+17日线　+3.86% / 64.4%",
        "个股 S13 KDJ低位金叉　+3.43% / 58.2%",
        "ETF S04 三倍量　+3.22% / 61.3%"]},
    "缩量+价涨": {"n": 0, "list": []},
    "缩量+价平": {"n": 0, "list": []},
    "缩量+价跌": {"n": 14, "list": [
        "个股 S14 MACD零轴下金叉　+3.41% / 57.4%",
        "ETF S06 三倍量+低位　+3.21% / 58.6%",
        "个股 S03 周线KDJ凹底+17日线　+3.12% / 58.3%",
        "ETF S05 三倍量+前3日下跌　+3.06% / 55.1%",
        "ETF S03 周线KDJ凹底+17日线　+2.82% / 65.9%"]},
}

# ── 行动卡：每一格具体该做什么（与《什么行情用什么方法》第 5、6 步一致）──
STRATEGY = {
    ("放量", "价涨"): {
        "do": "做趋势中继 —— 回踩过均线、今天重新走强的强势股",
        "pick": ["MA20 &gt; MA60 且 MA60 上行（趋势结构成立）",
                 "近 5 日内曾收盘跌破 MA20（有真实回踩，不是追高）",
                 "今日收盘 &gt; MA5 且收涨（重新走强）",
                 "收盘 ≥ MA20 × 1.02（别买在止损线上）",
                 "20 日日均成交额 ≥ 1 亿"],
        "size": "可用足额：单笔 6,000 元，最多同时 2 只",
        "avoid": ["不追高开超过 +3% 的",
                  "不碰高于 MA60 超过 +25% 的（位置否决项）"],
    },
    ("放量", "价平"): {
        "do": "做行业轮动的补涨方向 —— 钱在进场，但还没全面开花",
        "pick": ["选股条件同上（趋势结构 + 回踩 + 重新走强 + 缓冲）",
                 "优先选当日涨停家数最多的行业",
                 "同题材至少 3 家一起动才算主线"],
        "size": "单笔 6,000 元，最多同时 2 只",
        "avoid": ["不买单打独斗的票（孤零零一个涨停不做）",
                  "不做已经连涨三天以上的补涨"],
    },
    ("放量", "价跌"): {
        "do": "先观察止跌，不急着买 —— 放量下跌多半是恐慌盘",
        "pick": ["若要试，只做跌到 MA60 下方的超跌品种",
                 "要求当天出现「第一根放量阳线」（缩量下跌后的第一次转强）",
                 "等指数不再创新低才动手"],
        "size": "半仓：单笔 3,000 元，只做 1 只",
        "avoid": ["不接下落的飞刀（连续阴线中不买）",
                  "不做下跌中继（均线空头排列的）"],
    },
    ("平量", "价涨"): {
        "do": "轻仓试 —— 涨是涨了，但没有增量资金，力度不足",
        "pick": ["选股条件同「放量 + 价涨」那一格",
                 "额外要求：必须属于当日题材热度 TOP3"],
        "size": "单笔 ≤ 4,000 元，最多 1 只",
        "avoid": ["不重仓", "不追第二波（第一波没赶上就放弃）"],
    },
    ("平量", "价平"): {
        "do": "观望 —— 没有方向，不做比乱做好",
        "pick": ["不开新仓",
                 "把时间用来：跑选股器、更新观察池、整理交易记录",
                 "已有持仓：只按移动止损纪律管理"],
        "size": "0 元（空仓）",
        "avoid": ["不因为「闲着没事」而下单",
                  "不因为「这个票看起来不错」而破例"],
    },
    ("平量", "价跌"): {
        "do": "可以低吸 —— 找超跌的低位品种，但要分批",
        "pick": ["跌到 MA60 下方（位置低）",
                 "连续缩量后出现第一根放量阳线",
                 "确认不是高位股（高于 MA60 的一律排除）"],
        "size": "分两批：先建 1/3（2,000 元），信号确认后再补",
        "avoid": ["不满仓抄底", "不在下跌途中一路加仓"],
    },
    ("缩量", "价涨"): {
        "do": "不做 —— 无量上涨接不住，多为假突破",
        "pick": ["不开新仓",
                 "重点看：涨的这部分是「存量资金腾挪」还是「增量进场」",
                 "等成交额回到均线附近再评估"],
        "size": "0 元（空仓）",
        "avoid": ["尤其不追已经涨了一波的",
                  "不把「缩量上涨」当成「筹码锁定」的利好"],
    },
    ("缩量", "价平"): {
        "do": "不做 —— 九格里最差的一格，胜率只有 36%",
        "pick": ["不开新仓，把「等待」当成正式动作",
                 "盯两个信号：① 成交额回到 1.8 万亿以上（量能→平量）",
                 "② 或价格跌破 −3%（变成「缩量 + 价跌」那一格）"],
        "size": "0 元（空仓）",
        "avoid": ["缩量 + 价平 是最容易做多做错的状态",
                  "不做日内来回，不因为手痒而降标准"],
    },
    ("缩量", "价跌"): {
        "do": "准备做 —— 超跌反弹格（历史胜率 62%），但要等放量确认",
        "pick": ["先列观察名单：超跌、低位、基本面不难看的",
                 "等信号：缩量止跌 + 第一根放量阳线",
                 "分批：确认后先 1/2，站稳再加"],
        "size": "确认后单笔 6,000 元；未确认前 0 元",
        "avoid": ["不等确认就买＝赌（缩量还能继续缩，价格还能继续阴跌）",
                  "不因为「已经跌很多了」就认为不会再跌"],
    },
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
    strat = dict(STRATEGY.get((vb, pdir), {}))
    sk = STRAT_BY_REGIME.get("%s+%s" % (vb, pdir))
    if sk:
        strat["strat_n"] = sk["n"]
        strat["strat_list"] = sk["list"]

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
        "行动卡": strat,
        "九宫格速查": [
            {"格": "%s + %s" % (v2, p2),
             "后20日%": PANEL[(v2, p2)]["ret"],
             "胜率%": PANEL[(v2, p2)]["win"],
             "动作": PANEL[(v2, p2)]["act"],
             "色": PANEL[(v2, p2)]["col"],
             "当前": (v2 == vb and p2 == pdir)}
            for v2 in ("放量", "平量", "缩量") for p2 in ("价涨", "价平", "价跌")
        ],
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
