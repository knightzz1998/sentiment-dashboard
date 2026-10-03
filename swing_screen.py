# -*- coding: utf-8 -*-
"""波段选股（第 19/20 册口径）→ 供情绪看板每日自动任务调用

- 数据源：腾讯前复权日线（90 根），直连
- 缓存：系统临时目录（**不放项目目录**，否则会被打进部署包）
- 输出：data/swing.json（当日闸门 + 候选 + 阶段 1 进度）
         data/stage1_log.json（逐日追加，同日覆盖 —— 阶段 1 的 20 天记录自动化）

用法：python swing_screen.py            # 按 data/sentiment.json 的 updated 日期
      python swing_screen.py --offline  # 只用缓存，不联网
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "127.0.0.1,localhost"

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
CODES = DATA / "codes.json"
HIST = DATA / "sentiment.json"
SWING = DATA / "swing.json"
LOG = DATA / "stage1_log.json"
CACHE = Path(os.environ.get("TEMP", "/tmp")) / "swing_kline_cache.json"
NAMES = {}

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
TARGET_DAYS = 20          # 阶段 1 的目标交易日数


def tx_prefix(code: str) -> str:
    if code[:1] in ("6", "9"):
        return "sh" + code
    if code[:1] in ("0", "3"):
        return "sz" + code
    return "bj" + code


def em_secid(code: str) -> str:
    """东财 secid：沪市 1.，深市/北交所 0."""
    return ("1." if code[:1] in ("6", "9") else "0.") + code


def fetch_em(code: str, lmt: int = 90):
    """东财前复权日线（主源）→ [date, open, high, low, close, volume, amount]"""
    u = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s"
         "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
         "&klt=101&fqt=1&end=20500101&lmt=%d" % (em_secid(code), lmt))
    raw = urllib.request.urlopen(
        urllib.request.Request(u, headers=UA), timeout=20).read().decode("utf-8", "ignore")
    d = (json.loads(raw).get("data") or {})
    kl = d.get("klines") or []
    bars = []
    for s in kl:
        p = s.split(",")
        if len(p) < 8:
            continue
        try:
            bars.append([p[0], float(p[1]), float(p[3]), float(p[4]),
                         float(p[2]), float(p[5]), float(p[6])])
        except (ValueError, TypeError):
            continue
    return d.get("name") or "", (bars or None)


def fetch_tx(code: str, datalen: int = 90):
    """腾讯前复权日线（兜底；无成交额字段，用 量×100×收 估算）"""
    sym = tx_prefix(code)
    u = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq"
         % (sym, datalen))
    raw = urllib.request.urlopen(
        urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0",
                                           "Referer": "https://gu.qq.com/"}),
        timeout=20).read().decode("utf-8", "ignore")
    d = (json.loads(raw).get("data") or {}).get(sym) or {}
    arr = d.get("qfqday") or d.get("day") or []
    bars = []
    for r in arr:
        if len(r) < 6:
            continue
        try:
            o, c, h, lo, v = float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])
            bars.append([r[0], o, h, lo, c, v, v * 100 * c])
        except (ValueError, TypeError):
            continue
    return "", (bars or None)


def fetch_sina(code: str, datalen: int = 90):
    """新浪日线（第三兜底；**不复权**，有除权会被扭曲，仅在前两源都失败时用）"""
    u = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
         "CN_MarketData.getKLineData?symbol=%s&scale=240&ma=no&datalen=%d"
         % (tx_prefix(code), datalen))
    raw = urllib.request.urlopen(
        urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0",
                                           "Referer": "https://finance.sina.com.cn"}),
        timeout=15).read().decode("utf-8", "ignore")
    arr = json.loads(raw)
    bars = []
    for r in arr:
        try:
            o, h, lo, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
            v = float(r.get("volume") or 0)
            bars.append([r["day"], o, h, lo, c, v, v * 100 * c])
        except (KeyError, ValueError, TypeError):
            continue
    return "", (bars or None)


def fetch_one(code: str, datalen: int = 90):
    """依次尝试 东财 → 腾讯 → 新浪，任一成功即返回 (code, name, bars)"""
    for fn in (fetch_em, fetch_tx, fetch_sina):
        try:
            nm, bars = fn(code, datalen)
            if bars:
                return code, nm, bars
        except Exception:
            time.sleep(0.15)
    return code, "", None


def fetch_names(codes):
    out = {}
    ls = [tx_prefix(c) for c in codes]
    for k in range(0, len(ls), 60):
        try:
            req = urllib.request.Request(
                "http://hq.sinajs.cn/list=" + ",".join(ls[k:k + 60]),
                headers={"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=20).read().decode("gbk", "ignore")
            for m in re.finditer(r'hq_str_[a-z]{2}(\d{6})="([^,]*),', raw):
                out[m.group(1)] = m.group(2)
        except Exception:
            pass
    return out


def ma(vals, n, i):
    if i + 1 < n:
        return None
    return sum(vals[i - n + 1:i + 1]) / n


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    a = ap.parse_args()

    target = ""
    if HIST.exists():
        try:
            target = json.loads(HIST.read_text(encoding="utf-8")).get("updated", "")
        except Exception:
            target = ""
    codes = json.loads(CODES.read_text(encoding="utf-8"))
    data = {}
    if CACHE.exists():
        try:
            data = json.loads(CACHE.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    need = [] if a.offline else [c for c in codes if c not in data or not data[c]
                                 or (target and data[c][-1][0] < target)]
    if need:
        print("拉取 %d 只（东财前复权 → 腾讯 → 新浪，三级兜底）…" % len(need))
        t0 = time.time()
        done, bad = 0, 0
        with ThreadPoolExecutor(max_workers=6) as ex:
            for f in as_completed([ex.submit(fetch_one, c) for c in need]):
                code, nm, bars = f.result()
                if bars:
                    data[code] = bars
                    if nm:
                        NAMES[code] = nm
                else:
                    bad += 1
                done += 1
                if done % 500 == 0:
                    print("  %d/%d  成功 %d  失败 %d  %.0fs"
                          % (done, len(need), done - bad, bad, time.time() - t0))
        if data:
            try:
                CACHE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            except Exception as e:
                print("  缓存写入失败（不影响本次结果）：%s" % e)
        else:
            print("  全部失败，**不覆盖缓存**（避免把好缓存写成空文件）")
        print("  完成 %d 只（失败 %d，%.0fs）" % (len(data), bad, time.time() - t0))
    else:
        print("缓存已是最新（%d 只）" % len(data))

    days = defaultdict(int)
    for v in data.values():
        if v:
            days[v[-1][0]] += 1
    if not days:
        raise SystemExit("没有可用行情数据")
    last = max(days, key=lambda d: days[d])

    # ── 等权指数 → 大盘环境 ──
    sumret, cnt = defaultdict(float), defaultdict(int)
    for code, bars in data.items():
        for k in range(1, len(bars)):
            p0, p1 = bars[k - 1][4], bars[k][4]
            if p0 > 0 and p1 > 0:
                sumret[bars[k][0]] += (p1 / p0 - 1)
                cnt[bars[k][0]] += 1
    eqd = sorted(sumret)
    eql, eqi, lvl = [], {}, 1.0
    for i, d in enumerate(eqd):
        eqi[d] = i
        lvl *= (1 + (sumret[d] / cnt[d] if cnt[d] else 0.0))
        eql.append(lvl)
    chg20, regime = None, "未知"
    if last in eqi and eqi[last] >= 20:
        chg20 = round((eql[eqi[last]] / eql[eqi[last] - 20] - 1) * 100, 2)
        regime = "上涨" if chg20 > 3 else ("下跌" if chg20 < -3 else "震荡")
    gate_open = regime in ("上涨", "下跌")

    # ── 八级漏斗 ──
    fun = {"有数据": 0, "趋势结构": 0, "真实回踩": 0, "重新走强": 0,
           "够缓冲": 0, "位置合格": 0, "发散度合格": 0, "流动性合格": 0}
    cands = []
    for code, bars in data.items():
        i = len(bars) - 1
        if i < 75 or bars[i][0] != last:
            continue
        fun["有数据"] += 1
        close = [b[4] for b in bars]
        low = [b[3] for b in bars]
        m5, m20, m60 = ma(close, 5, i), ma(close, 20, i), ma(close, 60, i)
        m60p = ma(close, 60, i - 10)
        if None in (m5, m20, m60, m60p) or not (m20 > m60 and m60 > m60p):
            continue
        fun["趋势结构"] += 1
        if not any(close[k] < ma(close, 20, k) for k in range(i - 4, i + 1) if ma(close, 20, k)):
            continue
        fun["真实回踩"] += 1
        if not (close[i] > m5 and close[i] > close[i - 1]):
            continue
        fun["重新走强"] += 1
        if close[i] < m20 * 1.02:
            continue
        fun["够缓冲"] += 1
        dev60 = (close[i] / m60 - 1) * 100
        if dev60 > 25:
            continue
        fun["位置合格"] += 1
        disp = (m20 / m60 - 1) * 100
        if not (2 <= disp <= 5):
            continue
        fun["发散度合格"] += 1
        seg = bars[i - 19:i + 1]
        amt20 = sum((b[6] if len(b) > 6 else b[5] * 100 * b[4]) for b in seg) / 20.0
        if amt20 < 5e7:
            continue
        fun["流动性合格"] += 1
        cands.append({
            "code": code, "close": round(close[i], 2),
            "ma20": round(m20, 2), "ma60": round(m60, 2),
            "dev60": round(dev60, 2), "disp": round(disp, 2),
            "buf": round((close[i] / m20 - 1) * 100, 2),
            "amt": round(amt20 / 1e8, 2),
            "stop": round(close[i] * 0.9, 2),
            "add_at": round(m20, 2),
            "board": "主板" if (code[:1] in ("6", "0") and code[:3] not in
                              ("688", "689", "300", "301")) else "需权限",
        })

    names = dict(NAMES)
    miss = [c["code"] for c in cands if c["code"] not in names]
    if miss:
        names.update(fetch_names(miss))
    main_c, perm_c, st_c = [], [], []
    for c in cands:
        nm = names.get(c["code"], c["code"])
        c["name"] = nm
        if "ST" in nm.upper() or "退" in nm:
            st_c.append({"code": c["code"], "name": nm})
        elif c["board"] == "主板":
            main_c.append(c)
        else:
            perm_c.append(c)
    for c in main_c + perm_c:
        c["score"] = (2 if c["dev60"] <= 10 else 1) + (2 if 2 <= c["disp"] <= 5 else 0) \
            + (2 if c["amt"] >= 2 else 1)
    main_c.sort(key=lambda c: (-c["score"], -c["amt"]))
    perm_c.sort(key=lambda c: (-c["score"], -c["amt"]))

    # ── 阶段 1 逐日记录（同日覆盖，幂等）──
    log = []
    if LOG.exists():
        try:
            log = json.loads(LOG.read_text(encoding="utf-8"))
        except Exception:
            log = []
    log = [x for x in log if x.get("d") != last]
    log.append({"d": last, "regime": regime, "chg20": chg20,
                "gate": gate_open, "n": len(main_c), "codes": [c["code"] for c in main_c]})
    log.sort(key=lambda x: x["d"])
    LOG.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")

    out = {
        "date": last, "regime": regime, "chg20": chg20,
        "gate_open": gate_open,
        "gate_reason": ("闸门开放：%s行情（20 日 %+.2f%%）" % (regime, chg20 or 0)
                        if gate_open else
                        "闸门关闭：震荡行情（20 日 %+.2f%%，该档超额 −0.21%%、胜率 21.6%%）"
                        % (chg20 or 0)),
        "funnel": fun,
        "main": main_c, "need_perm": perm_c, "st": st_c,
        "stage1": {"target": TARGET_DAYS, "done": len(log),
                   "open_days": sum(1 for x in log if x["gate"]),
                   "rows": log[-20:]},
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": "腾讯前复权日线（90 根）｜口径：第 19 册《波段战法》第 2、3 章",
    }
    SWING.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    print("数据日期 %s｜大盘 %s（20 日 %+.2f%%）｜闸门 %s"
          % (last, regime, chg20 or 0, "开放" if gate_open else "关闭"))
    print("漏斗：" + " → ".join("%s %d" % (k, v) for k, v in fun.items()))
    print("主板候选 %d 只｜需权限 %d 只｜剔除 ST %d 只" % (len(main_c), len(perm_c), len(st_c)))
    print("阶段 1 进度：%d / %d 个交易日（其中闸门开放 %d 天）"
          % (len(log), TARGET_DAYS, sum(1 for x in log if x["gate"])))
    for c in main_c[:10]:
        print("  %s %-8s %.2f 位置%+.2f%% 发散%.2f%% 额%.2f亿"
              % (c["code"], c["name"][:8], c["close"], c["dev60"], c["disp"], c["amt"]))
    print("已写 %s" % SWING)


if __name__ == "__main__":
    main()
