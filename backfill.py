# -*- coding: utf-8 -*-
"""补齐缺失交易日的每日情绪指标（数据源：新浪日线）

场景：种子历史来自本机通达信缓存（截至某日），之后用实时快照更新会缺中间几天，
      导致「连板家数」失真。本脚本用新浪日线把中间缺失的交易日补齐。

用法：python backfill.py [--from YYYY-MM-DD]
"""
import argparse
import json
import sys
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
HIST = DATA / "sentiment.json"
CODES = DATA / "codes.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
WORKERS = 12


def limit_of(code):
    if code[:3] in ("688", "689", "300", "301"):
        return 19.6
    if code[:1] in ("4", "8") or code[:2] == "92":
        return 29.6
    return 9.6


def sym(code):
    if code[:1] == "6" or code[:3] in ("688", "689"):
        return "sh" + code
    if code[:1] in ("4", "8") or code[:2] == "92":
        return "bj" + code
    return "sz" + code


def fetch_one(code, datalen=25):
    u = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
         "CN_MarketData.getKLineData?symbol=" + sym(code) +
         "&scale=240&ma=no&datalen=" + str(datalen))
    req = urllib.request.Request(u, headers={"User-Agent": UA,
                                            "Referer": "https://finance.sina.com.cn"})
    for attempt in range(2):
        try:
            raw = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
            return code, json.loads(raw)
        except Exception:
            time.sleep(0.7 + attempt)
    return code, None


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="dfrom", default=None)
    ap.add_argument("--workers", type=int, default=WORKERS)
    a = ap.parse_args()

    obj = json.loads(HIST.read_text(encoding="utf-8"))
    daily = obj["daily"]
    have = {x["d"] for x in daily}
    codes = json.loads(CODES.read_text(encoding="utf-8"))
    last = max(have)
    print("已有 %d 天，最后一天 %s，开始拉取 %d 只的日线…" % (len(daily), last, len(codes)))

    bars_of = {}
    t0 = time.time()
    done = 0
    cache = DATA / "_kline_cache.json"
    if cache.exists():
        bars_of = json.loads(cache.read_text(encoding="utf-8"))
        print("用日线缓存 %d 只" % len(bars_of))
    else:
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            futs = [ex.submit(fetch_one, c) for c in codes]
            for f in as_completed(futs):
                code, data = f.result()
                if data:
                    bars_of[code] = data
                done += 1
                if done % 800 == 0:
                    print("  %d/%d  %.0fs" % (done, len(codes), time.time() - t0))
        cache.write_text(json.dumps(bars_of, ensure_ascii=False), encoding="utf-8")
        print("拿到 %d 只（%.0fs），已缓存" % (len(bars_of), time.time() - t0))

    # 按日期聚合
    by_date = defaultdict(list)
    for code, bars in bars_of.items():
        for i in range(1, len(bars)):
            try:
                pc = float(bars[i - 1]["close"])
                c = float(bars[i]["close"])
                h = float(bars[i]["high"])
            except (KeyError, ValueError):
                continue
            if pc <= 0:
                continue
            by_date[bars[i]["day"]].append((code, (c / pc - 1) * 100, (h / pc - 1) * 100))

    dates = sorted(d for d in by_date if d not in have and d <= last)
    if a.dfrom:
        dates = [d for d in dates if d >= a.dfrom]
    if not dates:
        print("没有需要补齐的交易日")
        return
    start = dates[0]
    # 补完后，后面已存在的记录（如最新的实时快照）也要用正确的前一日名单重算
    dates = sorted(d for d in by_date if start <= d <= last)
    print("待补日期：%s" % " ".join(dates))

    daily = [x for x in daily if x["d"] < start]
    prev_lu = set()
    for rec in reversed(daily):
        if rec.get("lu"):
            prev_lu = set(rec["lu"])
            break
    print("起始前一日涨停名单：%d 只" % len(prev_lu))

    for d in dates:
        rows = by_date[d]
        up = dn = touch = sealed = ups = downs = 0
        lu = []
        chgs = []
        for code, chg, hchg in rows:
            lim = limit_of(code)
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
        lu_set = set(lu)
        lb2 = len([c for c in lu if c in prev_lu])
        # 最高连板：回溯前 12 天（daily 已按日期升序，且当前日尚未 append）
        hgt = 1
        for c in lu:
            n = 1
            for rec in reversed(daily[-12:]):
                if c in set(rec.get("lu", [])):
                    n += 1
                else:
                    break
            hgt = max(hgt, n)
        rec = {"d": d, "up": up, "dn": dn, "touch": touch, "sealed": sealed,
               "ups": ups, "downs": downs, "lb2": lb2, "lu": sorted(lu),
               "br": round((touch - sealed) / touch, 4) if touch else 0.0,
               "eq": round(sum(chgs) / len(chgs), 4) if chgs else 0.0,
               "lbh": hgt if lu else 0, "backfill": True}
        daily.append(rec)
        prev_lu = lu_set
        print("  补 %s：涨停 %d / 跌停 %d / 连板 %d / 最高 %d 板（样本 %d 只）"
              % (d, up, dn, lb2, hgt, len(rows)))

    daily.sort(key=lambda x: x["d"])
    for x in daily[:-90]:
        x.pop("lu", None)
    obj["daily"] = daily
    obj["updated"] = daily[-1]["d"]
    HIST.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    print("已写回 %s（共 %d 天，最后 %s）" % (HIST.name, len(daily), obj["updated"]))


if __name__ == "__main__":
    main()
