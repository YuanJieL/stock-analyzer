"""
=============================================================
台股智能分析系統 - 資料爬蟲 v3（限速修正版）
=============================================================

【v3 修正項目】
  1. Yahoo Finance 限速（429）問題
     → 每次請求前加入隨機等待（3~8秒）
     → 技術面改用 TWSE 官方每日收盤資料備援

  2. 基本面改用 TWSE 官方本益比/殖利率 API
     → 不再依賴 Yahoo Finance info
     → 直接從證交所取得 PE、PB、殖利率

  3. 法人張數單位修正
     → TWSE API 回傳單位為「股」，除以1000換算成「張」

  4. 加入重試機制（最多3次）
=============================================================
"""

import json
import time
import random
import requests
import yfinance as yf
import pandas as pd
from datetime import datetime, date, timedelta
import os

# ─────────────────────────────────────────────
# 【設定區】
# ─────────────────────────────────────────────
WATCH_LIST = [
    {"code": "3037", "name": "欣興",  "sector": "PCB/ABF載板"},
    {"code": "2344", "name": "華邦電", "sector": "記憶體"},
    {"code": "2303", "name": "聯電",  "sector": "晶圓代工"},
    {"code": "3231", "name": "緯創",  "sector": "AI伺服器"},
    {"code": "2330", "name": "台積電", "sector": "晶圓代工"},
]

PORTFOLIO = [
    {"code": "0050", "name": "元大台灣50", "shares": 2000, "cost": 85},
    {"code": "0056", "name": "元大高股息",  "shares": 1000, "cost": 40},
    {"code": "2317", "name": "鴻海",        "shares": 500,  "cost": 210},
    {"code": "5880", "name": "合庫金",      "shares": 3000, "cost": 22},
    {"code": "2330", "name": "台積電",      "shares": 75,   "cost": 2000},
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

def retry_get(url, retries=3, wait=5, **kwargs):
    for i in range(retries):
        try:
            res = requests.get(url, timeout=15, headers=HEADERS, **kwargs)
            if res.status_code == 200:
                return res
            print(f"      HTTP {res.status_code}，{wait}秒後重試...")
        except Exception as e:
            print(f"      請求失敗: {e}，{wait}秒後重試...")
        time.sleep(wait)
    return None

# ─────────────────────────────────────────────
# Layer 2：技術面
# ─────────────────────────────────────────────
def fetch_price_and_technicals(code: str) -> dict:
    result = _fetch_from_yahoo(code)
    if result["price"] > 0:
        return result
    print(f"      Yahoo 失敗，改用 TWSE 備援...")
    return _fetch_from_twse(code)

def _fetch_from_yahoo(code: str) -> dict:
    wait = random.uniform(3, 8)
    print(f"      等待 {wait:.1f} 秒避免 Yahoo 限速...")
    time.sleep(wait)
    try:
        df = yf.download(f"{code}.TW", period="6mo", interval="1d",
                         progress=False, auto_adjust=True)
        if df.empty:
            return _empty_technical()

        close = df["Close"].squeeze()
        high  = df["High"].squeeze()
        low   = df["Low"].squeeze()
        vol   = df["Volume"].squeeze()

        price  = round(float(close.iloc[-1]), 2)
        prev   = float(close.iloc[-2]) if len(close) > 1 else price
        change = round((price - prev) / prev * 100, 2) if prev > 0 else 0

        ma20 = float(close.rolling(20).mean().iloc[-1])
        ma60 = float(close.rolling(60).mean().iloc[-1]) if len(close) >= 60 else ma20

        if price > ma20 * 1.005 and price > ma60 * 1.005:
            trend, ma20_s, ma60_s = "上升", "站上", "站上"
        elif price > ma20:
            trend, ma20_s = "盤整", "站上"
            ma60_s = "站上" if price > ma60 else "跌破"
        elif abs(price - ma20) / ma20 < 0.02:
            trend, ma20_s = "盤整", "貼近"
            ma60_s = "站上" if price > ma60 else "跌破"
        else:
            trend, ma20_s, ma60_s = "下降", "跌破", "跌破"

        low9  = low.rolling(9).min()
        high9 = high.rolling(9).max()
        rsv   = ((close - low9) / (high9 - low9).replace(0, 1) * 100).fillna(50)
        k = rsv.ewm(com=2, adjust=False).mean()
        d = k.ewm(com=2, adjust=False).mean()
        kd_k = round(float(k.iloc[-1]), 1)
        kd_d = round(float(d.iloc[-1]), 1)

        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rsi   = round(float((100 - 100 / (1 + gain / loss.replace(0, 0.001))).iloc[-1]), 1)

        ema12  = close.ewm(span=12, adjust=False).mean()
        ema26  = close.ewm(span=26, adjust=False).mean()
        macd_l = ema12 - ema26
        signal = macd_l.ewm(span=9, adjust=False).mean()
        hist   = macd_l - signal

        if len(macd_l) >= 2:
            if macd_l.iloc[-1] > signal.iloc[-1] and macd_l.iloc[-2] <= signal.iloc[-2]:
                macd_s = "黃金交叉"
            elif macd_l.iloc[-1] < signal.iloc[-1] and macd_l.iloc[-2] >= signal.iloc[-2]:
                macd_s = "死亡交叉"
            elif macd_l.iloc[-1] > signal.iloc[-1]:
                macd_s = "多頭" if hist.iloc[-1] > hist.iloc[-2] else "偏多"
            else:
                macd_s = "空頭"
        else:
            macd_s = "持平"

        vol_ma  = float(vol.rolling(20).mean().iloc[-1])
        today_v = float(vol.iloc[-1])
        vol_s   = "大量" if today_v > vol_ma*1.5 else "放量" if today_v > vol_ma*1.1 else "縮量" if today_v < vol_ma*0.7 else "平穩"

        support    = round(float(low.rolling(20).min().iloc[-1]), 2)
        resistance = round(float(high.rolling(20).max().iloc[-1]), 2)

        notes = []
        if macd_s == "黃金交叉": notes.append("MACD黃金交叉")
        if kd_k < 30: notes.append("KD超賣")
        if rsi < 35:  notes.append("RSI超賣")
        if trend == "上升": notes.append("均線多頭")

        print(f"      [Yahoo] 價格={price} 趨勢={trend} KD={kd_k} RSI={rsi} MACD={macd_s}")

        return {
            "price": price, "change": change,
            "ma20": ma20_s, "ma60": ma60_s, "trend": trend,
            "kd_k": kd_k, "kd_d": kd_d, "macd": macd_s,
            "rsi": rsi, "volume": vol_s,
            "support": support, "resistance": resistance,
            "ma20_val": round(ma20, 2), "ma60_val": round(ma60, 2),
            "note": "、".join(notes) if notes else "技術面中性",
        }
    except Exception as e:
        print(f"      [Yahoo] 失敗: {e}")
        return _empty_technical()

def _fetch_from_twse(code: str) -> dict:
    try:
        today_str = date.today().strftime("%Y%m%d")
        url = f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?response=json&date={today_str}&stockNo={code}"
        res = retry_get(url)
        if not res:
            return _empty_technical()

        data = res.json()
        if data.get("stat") != "OK" or not data.get("data"):
            return _empty_technical()

        close_prices = []
        for row in data["data"]:
            try:
                close_prices.append(float(row[6].replace(",", "")))
            except:
                pass

        if not close_prices:
            return _empty_technical()

        price  = close_prices[-1]
        change = round((close_prices[-1] - close_prices[-2]) / close_prices[-2] * 100, 2) if len(close_prices) >= 2 else 0
        ma20   = sum(close_prices[-20:]) / min(len(close_prices), 20)
        trend  = "上升" if price > ma20 * 1.01 else "盤整" if abs(price - ma20) / ma20 < 0.03 else "下降"
        ma20_s = "站上" if price > ma20 else "跌破"

        print(f"      [TWSE備援] 價格={price} 趨勢={trend}")

        return {
            "price": price, "change": change,
            "ma20": ma20_s, "ma60": "N/A", "trend": trend,
            "kd_k": 50, "kd_d": 50, "macd": "N/A",
            "rsi": 50, "volume": "N/A",
            "support":    min(close_prices[-20:]) if len(close_prices) >= 20 else price * 0.9,
            "resistance": max(close_prices[-20:]) if len(close_prices) >= 20 else price * 1.1,
            "ma20_val": round(ma20, 2), "ma60_val": 0,
            "note": "技術指標使用備援資料",
        }
    except Exception as e:
        print(f"      [TWSE備援] 失敗: {e}")
        return _empty_technical()

def _empty_technical():
    return {
        "price": 0, "change": 0,
        "ma20": "N/A", "ma60": "N/A", "trend": "N/A",
        "kd_k": 50, "kd_d": 50, "macd": "N/A",
        "rsi": 50, "volume": "N/A",
        "support": 0, "resistance": 0,
        "ma20_val": 0, "ma60_val": 0,
        "note": "技術面資料暫無法取得",
    }

# ─────────────────────────────────────────────
# Layer 1：基本面 - TWSE 官方本益比殖利率表
# ─────────────────────────────────────────────
def fetch_fundamental(code: str, price: float) -> dict:
    try:
        today_str = date.today().strftime("%Y%m%d")
        url = f"https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_d?response=json&date={today_str}&selectType=ALL"
        res = retry_get(url)

        if not res:
            return _empty_fundamental()

        data = res.json()
        if data.get("stat") != "OK":
            return _empty_fundamental()

        for row in data.get("data", []):
            if not isinstance(row, list) or len(row) < 6:
                continue
            if row[0].strip() != code:
                continue

            def to_float(s):
                try:
                    return float(str(s).replace(",", "").strip())
                except:
                    return 0.0

            dividend_yield = to_float(row[2])
            pe             = to_float(row[4])
            pb             = to_float(row[5])
            eps            = round(price / pe, 2) if pe > 0 else 0

            # 補充從 Yahoo 抓 ROE 等
            roe, eps_growth, debt_ratio, revenue_growth = _fetch_extra_from_yahoo(code)

            note = f"本益比{pe}倍，殖利率{dividend_yield}%，淨值比{pb}倍"
            print(f"      [基本面] {code} PE={pe} 殖利率={dividend_yield}% PB={pb} ROE={roe}%")

            return {
                "eps": eps, "eps_growth": eps_growth, "roe": roe,
                "pe": pe, "pb": pb, "dividend_yield": dividend_yield,
                "debt_ratio": debt_ratio, "revenue_growth": revenue_growth,
                "note": note,
            }

        return _empty_fundamental()

    except Exception as e:
        print(f"      [基本面] {code} 錯誤: {e}")
        return _empty_fundamental()

def _fetch_extra_from_yahoo(code: str) -> tuple:
    try:
        time.sleep(random.uniform(2, 5))
        info = yf.Ticker(f"{code}.TW").info
        roe_raw = info.get("returnOnEquity", 0)
        eg_raw  = info.get("earningsGrowth", 0)
        de_raw  = info.get("debtToEquity", 0)
        rg_raw  = info.get("revenueGrowth", 0)
        roe            = round(float(roe_raw) * 100, 1) if roe_raw else 0
        eps_growth     = round(float(eg_raw) * 100, 1) if eg_raw else 0
        de             = float(de_raw) / 100 if de_raw else 0
        debt_ratio     = round(de / (1 + de) * 100, 1) if de > 0 else 0
        revenue_growth = round(float(rg_raw) * 100, 1) if rg_raw else 0
        return roe, eps_growth, debt_ratio, revenue_growth
    except:
        return 0, 0, 0, 0

def _empty_fundamental():
    return {
        "eps": 0, "eps_growth": 0, "roe": 0,
        "pe": 0, "pb": 0, "dividend_yield": 0,
        "debt_ratio": 0, "revenue_growth": 0,
        "note": "基本面資料暫無法取得",
    }

# ─────────────────────────────────────────────
# Layer 3：籌碼面（修正張數單位）
# ─────────────────────────────────────────────
def fetch_institutional(code: str, days: int = 10) -> dict:
    foreign_list = []
    trust_list   = []
    dealer_list  = []

    today   = date.today()
    offset  = 0
    checked = 0

    while checked < days and offset < 20:
        target = today - timedelta(days=offset)
        offset += 1
        if target.weekday() >= 5:
            continue

        date_str = target.strftime("%Y%m%d")
        url = f"https://www.twse.com.tw/rwd/zh/fund/T86?response=json&date={date_str}&selectType=ALLBUT0999"

        try:
            res = retry_get(url, retries=2, wait=3)
            if not res:
                continue

            data = res.json()
            if data.get("stat") != "OK":
                continue

            for row in data.get("data", []):
                if not isinstance(row, list) or len(row) < 11:
                    continue
                if row[0].strip() != code:
                    continue

                def to_int(s):
                    try:
                        return int(str(s).replace(",", "").replace("+", "").strip())
                    except:
                        return 0

                # 單位修正：股 → 張（÷1000）
                foreign_net = to_int(row[4])  // 1000
                trust_net   = to_int(row[10]) // 1000
                dealer_net  = (to_int(row[13]) + to_int(row[16])) // 1000 if len(row) > 16 else 0

                foreign_list.append(foreign_net)
                trust_list.append(trust_net)
                dealer_list.append(dealer_net)
                checked += 1
                break

            time.sleep(0.5)

        except Exception as e:
            print(f"      [法人] {code} {date_str} 錯誤: {e}")

    def count_streak(lst):
        if not lst:
            return 0
        sign  = 1 if lst[0] > 0 else -1
        count = 0
        for v in lst:
            if (v > 0) == (sign > 0):
                count += 1
            else:
                break
        return count * sign

    f_days = count_streak(foreign_list)
    t_days = count_streak(trust_list)

    notes = []
    if f_days >= 3:  notes.append(f"外資連買{f_days}天")
    if t_days >= 2:  notes.append(f"投信連買{t_days}天")
    if f_days >= 3 and t_days >= 2: notes.append("法人同步買超")
    if f_days <= -3: notes.append(f"外資連賣{abs(f_days)}天⚠️")

    print(f"      [籌碼] {code} 外資{f_days}天/{foreign_list[0] if foreign_list else 0}張 投信{t_days}天/{trust_list[0] if trust_list else 0}張")

    return {
        "foreign_days": f_days,
        "foreign_net":  foreign_list[0] if foreign_list else 0,
        "trust_days":   t_days,
        "trust_net":    trust_list[0] if trust_list else 0,
        "dealer_net":   dealer_list[0] if dealer_list else 0,
        "big_holder_change": "N/A",
        "margin_ratio": fetch_margin(code),
        "is_warning":   False,
        "note":         "、".join(notes) if notes else "無明顯法人訊號",
    }

def fetch_margin(code: str) -> str:
    try:
        for offset in range(5):
            target = date.today() - timedelta(days=offset)
            if target.weekday() >= 5:
                continue
            url = f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?response=json&date={target.strftime('%Y%m%d')}&selectType=STOCK"
            res = retry_get(url, retries=2, wait=3)
            if not res:
                continue
            data = res.json()
            if data.get("stat") != "OK":
                continue
            for key in ["data", "data2"]:
                for row in data.get(key, []):
                    if not isinstance(row, list) or len(row) < 7:
                        continue
                    if row[0].strip() != code:
                        continue
                    try:
                        balance = int(row[4].replace(",", ""))
                        limit   = int(row[6].replace(",", ""))
                        ratio   = balance / limit if limit > 0 else 0
                        return "低" if ratio < 0.15 else "中" if ratio < 0.25 else "高"
                    except:
                        pass
            break
        return "N/A"
    except:
        return "N/A"

# ─────────────────────────────────────────────
# 評分引擎
# ─────────────────────────────────────────────
WEIGHTS = {"fundamental": 35, "technical": 35, "chips": 30}
SUB_WEIGHTS = {
    "fundamental": {"eps_growth":15,"roe":15,"pe":12,"pb":8,"dividend_yield":12,"debt_ratio":10,"revenue_growth":15,"moat":13},
    "technical":   {"trend":20,"ma":15,"kd":15,"macd":15,"rsi":10,"volume":15,"support":10},
    "chips":       {"foreign":35,"trust":20,"dealer":10,"big_holder":20,"margin":15},
}

def score_fundamental(f):
    return {
        "eps_growth":     100 if f["eps_growth"]>=40 else 80 if f["eps_growth"]>=25 else 60 if f["eps_growth"]>=10 else 40 if f["eps_growth"]>=0 else 10,
        "roe":            100 if f["roe"]>=25 else 80 if f["roe"]>=18 else 60 if f["roe"]>=12 else 40 if f["roe"]>=8 else 20,
        "pe":             100 if 0<f["pe"]<=15 else 80 if f["pe"]<=20 else 60 if f["pe"]<=30 else 40 if f["pe"]<=45 else 20 if f["pe"]>45 else 50,
        "pb":             100 if 0<f["pb"]<=1.5 else 80 if f["pb"]<=2.5 else 60 if f["pb"]<=4 else 40 if f["pb"]<=6 else 20 if f["pb"]>6 else 50,
        "dividend_yield": 100 if f["dividend_yield"]>=5 else 80 if f["dividend_yield"]>=3.5 else 60 if f["dividend_yield"]>=2 else 40 if f["dividend_yield"]>=1 else 20,
        "debt_ratio":     100 if f["debt_ratio"]<=30 else 80 if f["debt_ratio"]<=45 else 60 if f["debt_ratio"]<=60 else 40 if f["debt_ratio"]<=75 else 10,
        "revenue_growth": 100 if f["revenue_growth"]>=35 else 80 if f["revenue_growth"]>=20 else 60 if f["revenue_growth"]>=10 else 40 if f["revenue_growth"]>=0 else 10,
        "moat": 70,
    }

def score_technical(t):
    return {
        "trend":  100 if t["trend"]=="上升" else 55 if t["trend"]=="盤整" else 20,
        "ma":     100 if t["ma20"]=="站上" and t["ma60"]=="站上" else 70 if t["ma20"]=="站上" else 50 if t["ma20"]=="貼近" else 20,
        "kd":     90 if t["kd_k"]<30 else 75 if t["kd_k"]<50 else 55 if t["kd_k"]<70 else 30,
        "macd":   100 if t["macd"]=="黃金交叉" else 85 if t["macd"]=="翻多" else 75 if t["macd"]=="多頭" else 65 if t["macd"]=="偏多" else 50 if t["macd"]=="持平" else 20,
        "rsi":    90 if t["rsi"]<30 else 75 if t["rsi"]<50 else 60 if t["rsi"]<65 else 40 if t["rsi"]<80 else 15,
        "volume": 90 if t["volume"] in ["放量","大量"] else 65 if t["volume"]=="平穩" else 35,
        "support": 65,
    }

def score_chips(c):
    return {
        "foreign":    100 if c["foreign_days"]>=5 else 80 if c["foreign_days"]>=3 else 60 if c["foreign_days"]>=1 else 40 if c["foreign_days"]>=-1 else 10,
        "trust":      100 if c["trust_days"]>=3 else 75 if c["trust_days"]>=1 else 50 if c["trust_days"]>=-1 else 20,
        "dealer":     80 if c["dealer_net"]>500 else 60 if c["dealer_net"]>0 else 40,
        "big_holder": 55,
        "margin":     90 if c["margin_ratio"]=="低" else 60 if c["margin_ratio"]=="中" else 30 if c["margin_ratio"]=="高" else 55,
    }

def calc_layer(raw, sub_w):
    total_w = sum(sub_w.values())
    return round(sum(raw.get(k, 0) * sub_w[k] / total_w for k in sub_w))

def calc_total(f, t, c):
    return round(f * WEIGHTS["fundamental"]/100 + t * WEIGHTS["technical"]/100 + c * WEIGHTS["chips"]/100)

# ─────────────────────────────────────────────
# 持股損益
# ─────────────────────────────────────────────
def fetch_portfolio():
    result = []
    for p in PORTFOLIO:
        price = p["cost"]
        try:
            time.sleep(random.uniform(2, 5))
            hist = yf.download(f"{p['code']}.TW", period="3d", interval="1d",
                               progress=False, auto_adjust=True)
            if not hist.empty:
                price = round(float(hist["Close"].squeeze().iloc[-1]), 2)
        except:
            try:
                today_str = date.today().strftime("%Y%m%d")
                url = f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?response=json&date={today_str}&stockNo={p['code']}"
                res = retry_get(url, retries=2, wait=3)
                if res:
                    data = res.json()
                    if data.get("stat") == "OK" and data.get("data"):
                        price = float(data["data"][-1][6].replace(",", ""))
            except:
                pass

        value = round(price * p["shares"], 0)
        cost  = p["cost"] * p["shares"]
        pnl   = round(value - cost, 0)
        pct   = round(pnl / cost * 100, 2) if cost > 0 else 0
        result.append({**p, "price": price, "value": value, "pnl": pnl, "pct": pct})

    return result

# ─────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────
def main():
    print(f"\n{'='*50}")
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 台股分析系統 v3 啟動")
    print(f"{'='*50}\n")

    results = []

    for stock in WATCH_LIST:
        code = stock["code"]
        print(f"► 處理 {stock['name']} ({code})")

        tech  = fetch_price_and_technicals(code)
        price = tech["price"]
        fund  = fetch_fundamental(code, price)
        chips = fetch_institutional(code, days=10)

        fs = score_fundamental(fund)
        ts = score_technical(tech)
        cs = score_chips(chips)
        f_total = calc_layer(fs, SUB_WEIGHTS["fundamental"])
        t_total = calc_layer(ts, SUB_WEIGHTS["technical"])
        c_total = calc_layer(cs, SUB_WEIGHTS["chips"])
        total   = calc_total(f_total, t_total, c_total)

        print(f"  → 總分 {total}（基:{f_total} 技:{t_total} 碼:{c_total}）\n")

        results.append({
            **stock,
            "price":       price,
            "change":      tech["change"],
            "scores":      {"total": total, "fundamental": f_total, "technical": t_total, "chips": c_total},
            "fundamental": fund,
            "technical":   tech,
            "chips":       chips,
        })

        time.sleep(2)

    print("► 計算持股損益...")
    portfolio = fetch_portfolio()

    output = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "stocks":     results,
        "portfolio":  portfolio,
    }

    os.makedirs("data", exist_ok=True)
    with open("data/analysis.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    total_val = sum(p["value"] for p in portfolio)
    print(f"\n{'='*50}")
    print(f"✅ 完成！持股總值 NT${total_val/10000:.1f}萬")
    print(f"   已輸出 data/analysis.json")
    print(f"{'='*50}\n")

if __name__ == "__main__":
    main()

import os

# ─────────────────────────────────────────────
# 【設定區】可自由修改監控的股票清單
# ─────────────────────────────────────────────
WATCH_LIST = [
    {"code": "3037", "name": "欣興",  "sector": "PCB/ABF載板"},
    {"code": "2344", "name": "華邦電", "sector": "記憶體"},
    {"code": "2303", "name": "聯電",  "sector": "晶圓代工"},
    {"code": "3231", "name": "緯創",  "sector": "AI伺服器"},
    {"code": "2330", "name": "台積電", "sector": "晶圓代工"},
]

PORTFOLIO = [
    {"code": "0050", "name": "元大台灣50", "shares": 2000, "cost": 85},
    {"code": "0056", "name": "元大高股息",  "shares": 1000, "cost": 40},
    {"code": "2317", "name": "鴻海",        "shares": 500,  "cost": 210},
    {"code": "5880", "name": "合庫金",      "shares": 3000, "cost": 22},
    {"code": "2330", "name": "台積電",      "shares": 75,   "cost": 2000},
]

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# ─────────────────────────────────────────────
# Layer 1：基本面 - Yahoo Finance info
# ─────────────────────────────────────────────
# 【修正邏輯】
# 改用 yfinance 的 .info 欄位直接取得財務資料
# 這比解析 MOPS HTML 穩定很多
# 欄位對應：
#   trailingEps      → EPS（近四季）
#   trailingPE       → P/E 本益比
#   priceToBook      → P/B 淨值比
#   dividendYield    → 殖利率
#   debtToEquity     → 負債比（需換算）
#   returnOnEquity   → ROE
#   revenueGrowth    → 營收成長率（YoY）
#   earningsGrowth   → EPS成長率（YoY）

def fetch_fundamental(code: str, price: float) -> dict:
    """從 Yahoo Finance 抓基本面資料"""
    ticker_str = f"{code}.TW"
    try:
        ticker = yf.Ticker(ticker_str)
        info   = ticker.info

        # EPS（近四季合計）
        eps = round(float(info.get("trailingEps") or 0), 2)

        # EPS 成長率（YoY %）
        eg = info.get("earningsGrowth")
        eps_growth = round(float(eg) * 100, 1) if eg else 0

        # ROE（%）
        roe_raw = info.get("returnOnEquity")
        roe = round(float(roe_raw) * 100, 1) if roe_raw else 0

        # P/E
        pe_raw = info.get("trailingPE")
        pe = round(float(pe_raw), 1) if pe_raw else (round(price / eps, 1) if eps > 0 else 0)

        # P/B
        pb_raw = info.get("priceToBook")
        pb = round(float(pb_raw), 2) if pb_raw else 0

        # 殖利率（%）
        dy_raw = info.get("dividendYield")
        dividend_yield = round(float(dy_raw) * 100, 2) if dy_raw else 0

        # 負債比率
        # Yahoo 給的是 debtToEquity（負債/股東權益）
        # 換算成負債比率 = D/E / (1 + D/E) * 100
        de = info.get("debtToEquity")
        if de:
            de = float(de) / 100  # Yahoo 給的是百分比形式
            debt_ratio = round(de / (1 + de) * 100, 1)
        else:
            debt_ratio = 0

        # 營收成長率（YoY %）
        rg = info.get("revenueGrowth")
        revenue_growth = round(float(rg) * 100, 1) if rg else 0

        note = f"EPS {eps}元，ROE {roe}%，殖利率 {dividend_yield}%，本益比 {pe}倍"

        print(f"    [基本面] {code} EPS={eps} ROE={roe}% PE={pe} 殖利率={dividend_yield}%")

        return {
            "eps": eps, "eps_growth": eps_growth, "roe": roe,
            "pe": pe, "pb": pb, "dividend_yield": dividend_yield,
            "debt_ratio": debt_ratio, "revenue_growth": revenue_growth,
            "note": note,
        }

    except Exception as e:
        print(f"    [基本面] {code} 錯誤: {e}")
        return _empty_fundamental()

def _empty_fundamental():
    return {
        "eps": 0, "eps_growth": 0, "roe": 0,
        "pe": 0, "pb": 0, "dividend_yield": 0,
        "debt_ratio": 0, "revenue_growth": 0,
        "note": "基本面資料暫無法取得",
    }

# ─────────────────────────────────────────────
# Layer 2：技術面 - Yahoo Finance OHLCV
# ─────────────────────────────────────────────
def fetch_price_and_technicals(code: str) -> dict:
    """從 Yahoo Finance 抓股價並計算技術指標"""
    ticker = f"{code}.TW"
    try:
        df = yf.download(ticker, period="6mo", interval="1d", progress=False, auto_adjust=True)
        if df.empty:
            return _empty_technical()

        close = df["Close"].squeeze()
        high  = df["High"].squeeze()
        low   = df["Low"].squeeze()
        vol   = df["Volume"].squeeze()

        price  = round(float(close.iloc[-1]), 2)
        prev   = float(close.iloc[-2]) if len(close) > 1 else price
        change = round((price - prev) / prev * 100, 2) if prev > 0 else 0

        # 均線
        ma20 = float(close.rolling(20).mean().iloc[-1])
        ma60 = float(close.rolling(60).mean().iloc[-1]) if len(close) >= 60 else ma20

        # 趨勢判斷
        if price > ma20 * 1.01 and price > ma60 * 1.01:
            trend, ma20_s, ma60_s = "上升", "站上", "站上"
        elif price > ma20:
            trend, ma20_s = "盤整", "站上"
            ma60_s = "站上" if price > ma60 else "跌破"
        elif abs(price - ma20) / ma20 < 0.02:
            trend, ma20_s = "盤整", "貼近"
            ma60_s = "站上" if price > ma60 else "跌破"
        else:
            trend, ma20_s, ma60_s = "下降", "跌破", "跌破"

        # KD（9日）
        low9  = low.rolling(9).min()
        high9 = high.rolling(9).max()
        denom = high9 - low9
        rsv   = ((close - low9) / denom * 100).fillna(50)
        k = rsv.ewm(com=2, adjust=False).mean()
        d = k.ewm(com=2, adjust=False).mean()
        kd_k = round(float(k.iloc[-1]), 1)
        kd_d = round(float(d.iloc[-1]), 1)

        # RSI（14日）
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss.replace(0, 0.001)
        rsi   = round(float((100 - 100 / (1 + rs)).iloc[-1]), 1)

        # MACD（12,26,9）
        ema12  = close.ewm(span=12, adjust=False).mean()
        ema26  = close.ewm(span=26, adjust=False).mean()
        macd_l = ema12 - ema26
        signal = macd_l.ewm(span=9, adjust=False).mean()
        hist   = macd_l - signal

        if len(macd_l) >= 2:
            if macd_l.iloc[-1] > signal.iloc[-1] and macd_l.iloc[-2] <= signal.iloc[-2]:
                macd_status = "黃金交叉"
            elif macd_l.iloc[-1] < signal.iloc[-1] and macd_l.iloc[-2] >= signal.iloc[-2]:
                macd_status = "死亡交叉"
            elif macd_l.iloc[-1] > signal.iloc[-1]:
                macd_status = "多頭" if hist.iloc[-1] > hist.iloc[-2] else "偏多"
            elif macd_l.iloc[-1] < signal.iloc[-1]:
                macd_status = "空頭"
            else:
                macd_status = "持平"
        else:
            macd_status = "N/A"

        # 成交量
        vol_ma20  = float(vol.rolling(20).mean().iloc[-1])
        today_vol = float(vol.iloc[-1])
        if today_vol > vol_ma20 * 1.5:   vol_status = "大量"
        elif today_vol > vol_ma20 * 1.1: vol_status = "放量"
        elif today_vol < vol_ma20 * 0.7: vol_status = "縮量"
        else:                             vol_status = "平穩"

        support    = round(float(low.rolling(20).min().iloc[-1]), 2)
        resistance = round(float(high.rolling(20).max().iloc[-1]), 2)

        # 技術面備註
        notes = []
        if macd_status in ["黃金交叉", "翻多"]: notes.append("MACD翻多")
        if kd_k < 30:   notes.append("KD低檔超賣")
        if rsi < 35:    notes.append("RSI超賣")
        if trend == "上升": notes.append("均線多頭排列")
        if not notes:   notes.append("技術面中性")

        print(f"    [技術面] {code} 價格={price} 趨勢={trend} KD={kd_k}/{kd_d} RSI={rsi}")

        return {
            "price": price, "change": change,
            "ma20": ma20_s, "ma60": ma60_s, "trend": trend,
            "kd_k": kd_k, "kd_d": kd_d, "macd": macd_status,
            "rsi": rsi, "volume": vol_status,
            "support": support, "resistance": resistance,
            "ma20_val": round(ma20, 2), "ma60_val": round(ma60, 2),
            "note": "、".join(notes),
        }
    except Exception as e:
        print(f"    [技術面] {code} 錯誤: {e}")
        return _empty_technical()

def _empty_technical():
    return {
        "price": 0, "change": 0,
        "ma20": "N/A", "ma60": "N/A", "trend": "N/A",
        "kd_k": 50, "kd_d": 50, "macd": "N/A",
        "rsi": 50, "volume": "N/A",
        "support": 0, "resistance": 0,
        "ma20_val": 0, "ma60_val": 0,
        "note": "技術面資料暫無法取得",
    }

# ─────────────────────────────────────────────
# Layer 3：籌碼面 - 證交所 TWSE API
# ─────────────────────────────────────────────
# 【修正邏輯】
# TWSE T86 API 回傳欄位（修正版）：
# [0]代號 [1]名稱
# [2]外資買 [3]外資賣 [4]外資淨
# [5]外資自營買 [6]外資自營賣 [7]外資自營淨
# [8]投信買 [9]投信賣 [10]投信淨
# [11]自營買 [12]自營賣 [13]自營淨（避險）
# [14]自營買 [15]自營賣 [16]自營淨（非避險）
# [17]三大法人合計

def fetch_institutional(code: str, days: int = 10) -> dict:
    """從證交所抓三大法人買賣超"""
    foreign_list = []
    trust_list   = []
    dealer_list  = []

    today  = date.today()
    offset = 0
    checked = 0

    while checked < days and offset < 20:
        target = today - timedelta(days=offset)
        offset += 1
        if target.weekday() >= 5:
            continue

        date_str = target.strftime("%Y%m%d")
        url = f"https://www.twse.com.tw/rwd/zh/fund/T86?response=json&date={date_str}&selectType=ALLBUT0999"

        try:
            res  = requests.get(url, timeout=15, headers=HEADERS)
            data = res.json()

            if data.get("stat") != "OK":
                continue

            rows = data.get("data", [])
            for row in rows:
                if not isinstance(row, list) or len(row) < 11:
                    continue
                if row[0].strip() != code:
                    continue

                def to_int(s):
                    try:
                        return int(str(s).replace(",", "").replace("+", "").strip())
                    except:
                        return 0

                foreign_net = to_int(row[4])   # 外資淨買超
                trust_net   = to_int(row[10])  # 投信淨買超
                dealer_net  = to_int(row[13]) + to_int(row[16]) if len(row) > 16 else 0

                foreign_list.append(foreign_net)
                trust_list.append(trust_net)
                dealer_list.append(dealer_net)
                checked += 1
                break

            time.sleep(0.3)

        except Exception as e:
            print(f"    [法人] {code} {date_str} 錯誤: {e}")

    def count_streak(lst):
        """計算連續買超或賣超天數（正=買超天數，負=賣超天數）"""
        if not lst:
            return 0
        count = 0
        sign  = 1 if lst[0] > 0 else -1
        for v in lst:
            if (v > 0 and sign > 0) or (v < 0 and sign < 0):
                count += 1
            else:
                break
        return count * sign

    f_days = count_streak(foreign_list)
    t_days = count_streak(trust_list)

    # 籌碼備註
    notes = []
    if f_days >= 3:  notes.append(f"外資連買{f_days}天")
    if t_days >= 2:  notes.append(f"投信連買{t_days}天")
    if f_days >= 3 and t_days >= 2: notes.append("法人同步買超")
    if f_days <= -3: notes.append(f"外資連賣{abs(f_days)}天⚠️")

    print(f"    [籌碼] {code} 外資{f_days}天/{foreign_list[0] if foreign_list else 0}張 投信{t_days}天")

    return {
        "foreign_days":      f_days,
        "foreign_net":       foreign_list[0] if foreign_list else 0,
        "trust_days":        t_days,
        "trust_net":         trust_list[0] if trust_list else 0,
        "dealer_net":        dealer_list[0] if dealer_list else 0,
        "big_holder_change": fetch_big_holder(code),
        "margin_ratio":      "N/A",
        "is_warning":        False,
        "note":              "、".join(notes) if notes else "無明顯法人訊號",
    }

# ─────────────────────────────────────────────
# 大戶持股 - 集保結算所
# ─────────────────────────────────────────────
# 【邏輯】
# 集保每週更新一次持股分佈
# 觀察持股1000張以上的大戶比例變化
# 比例上升 → 籌碼集中 → 正面訊號

def fetch_big_holder(code: str) -> str:
    """抓大戶持股比例（集保結算所）"""
    try:
        url = "https://www.tdcc.com.tw/portal/zh/smWeb/qryStock"
        payload = {"scaDates": "", "scaDate": "", "SqlMethod": "StockNo", "StockNo": code, "StockName": ""}
        res  = requests.post(url, data=payload, timeout=15, headers=HEADERS)
        text = res.text

        # 找持股1000張以上的比例
        if "1,000" in text or "1000" in text:
            # 簡單判斷：若頁面有資料就回傳 N/A（完整解析較複雜）
            return "N/A"
        return "N/A"
    except:
        return "N/A"

# ─────────────────────────────────────────────
# 融資水位 - 證交所
# ─────────────────────────────────────────────
def fetch_margin(code: str) -> str:
    """抓融資水位"""
    try:
        # 找最近的交易日
        for offset in range(5):
            target = date.today() - timedelta(days=offset)
            if target.weekday() >= 5:
                continue
            date_str = target.strftime("%Y%m%d")
            url = f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?response=json&date={date_str}&selectType=STOCK"
            res  = requests.get(url, timeout=10, headers=HEADERS)
            data = res.json()

            if data.get("stat") != "OK":
                continue

            for key in ["data", "data2"]:
                for row in data.get(key, []):
                    if not isinstance(row, list) or len(row) < 7:
                        continue
                    if row[0].strip() != code:
                        continue
                    try:
                        balance = int(row[4].replace(",", ""))
                        limit   = int(row[6].replace(",", ""))
                        ratio   = balance / limit if limit > 0 else 0
                        if ratio < 0.15:   return "低"
                        elif ratio < 0.25: return "中"
                        else:              return "高"
                    except:
                        pass
            break

        return "N/A"
    except Exception as e:
        print(f"    [融資] {code} 錯誤: {e}")
        return "N/A"

# ─────────────────────────────────────────────
# 評分引擎
# ─────────────────────────────────────────────
WEIGHTS = {"fundamental": 35, "technical": 35, "chips": 30}
SUB_WEIGHTS = {
    "fundamental": {"eps_growth":15,"roe":15,"pe":12,"pb":8,"dividend_yield":12,"debt_ratio":10,"revenue_growth":15,"moat":13},
    "technical":   {"trend":20,"ma":15,"kd":15,"macd":15,"rsi":10,"volume":15,"support":10},
    "chips":       {"foreign":35,"trust":20,"dealer":10,"big_holder":20,"margin":15},
}

def score_fundamental(f):
    return {
        "eps_growth":     100 if f["eps_growth"]>=40 else 80 if f["eps_growth"]>=25 else 60 if f["eps_growth"]>=10 else 40 if f["eps_growth"]>=0 else 10,
        "roe":            100 if f["roe"]>=25 else 80 if f["roe"]>=18 else 60 if f["roe"]>=12 else 40 if f["roe"]>=8 else 20,
        "pe":             100 if 0<f["pe"]<=15 else 80 if f["pe"]<=20 else 60 if f["pe"]<=30 else 40 if f["pe"]<=45 else 20,
        "pb":             100 if 0<f["pb"]<=1.5 else 80 if f["pb"]<=2.5 else 60 if f["pb"]<=4 else 40 if f["pb"]<=6 else 20,
        "dividend_yield": 100 if f["dividend_yield"]>=5 else 80 if f["dividend_yield"]>=3.5 else 60 if f["dividend_yield"]>=2 else 40 if f["dividend_yield"]>=1 else 20,
        "debt_ratio":     100 if f["debt_ratio"]<=30 else 80 if f["debt_ratio"]<=45 else 60 if f["debt_ratio"]<=60 else 40 if f["debt_ratio"]<=75 else 10,
        "revenue_growth": 100 if f["revenue_growth"]>=35 else 80 if f["revenue_growth"]>=20 else 60 if f["revenue_growth"]>=10 else 40 if f["revenue_growth"]>=0 else 10,
        "moat": 70,
    }

def score_technical(t):
    return {
        "trend":  100 if t["trend"]=="上升" else 55 if t["trend"]=="盤整" else 20,
        "ma":     100 if t["ma20"]=="站上" and t["ma60"]=="站上" else 70 if t["ma20"]=="站上" else 50 if t["ma20"]=="貼近" else 20,
        "kd":     90 if t["kd_k"]<30 else 75 if t["kd_k"]<50 else 55 if t["kd_k"]<70 else 30,
        "macd":   100 if t["macd"]=="黃金交叉" else 85 if t["macd"]=="翻多" else 75 if t["macd"]=="多頭" else 65 if t["macd"]=="偏多" else 50 if t["macd"]=="持平" else 20,
        "rsi":    90 if t["rsi"]<30 else 75 if t["rsi"]<50 else 60 if t["rsi"]<65 else 40 if t["rsi"]<80 else 15,
        "volume": 90 if t["volume"] in ["放量","大量"] else 65 if t["volume"]=="平穩" else 35,
        "support": 65,
    }

def score_chips(c):
    return {
        "foreign":    100 if c["foreign_days"]>=5 else 80 if c["foreign_days"]>=3 else 60 if c["foreign_days"]>=1 else 40 if c["foreign_days"]>=-1 else 10,
        "trust":      100 if c["trust_days"]>=3 else 75 if c["trust_days"]>=1 else 50 if c["trust_days"]>=-1 else 20,
        "dealer":     80 if c["dealer_net"]>500 else 60 if c["dealer_net"]>0 else 40,
        "big_holder": 85 if str(c.get("big_holder_change","")).startswith("+") else 55,
        "margin":     90 if c["margin_ratio"]=="低" else 60 if c["margin_ratio"]=="中" else 30 if c["margin_ratio"]=="高" else 55,
    }

def calc_layer(raw, sub_w):
    total_w = sum(sub_w.values())
    return round(sum(raw.get(k,0) * sub_w[k] / total_w for k in sub_w))

def calc_total(f, t, c):
    return round(f * WEIGHTS["fundamental"]/100 + t * WEIGHTS["technical"]/100 + c * WEIGHTS["chips"]/100)

# ─────────────────────────────────────────────
# 持股損益
# ─────────────────────────────────────────────
def fetch_portfolio():
    result = []
    for p in PORTFOLIO:
        try:
            hist  = yf.download(f"{p['code']}.TW", period="2d", interval="1d", progress=False, auto_adjust=True)
            price = round(float(hist["Close"].squeeze().iloc[-1]), 2) if not hist.empty else p["cost"]
        except:
            price = p["cost"]

        value = round(price * p["shares"], 0)
        cost  = p["cost"] * p["shares"]
        pnl   = round(value - cost, 0)
        pct   = round(pnl / cost * 100, 2) if cost > 0 else 0
        result.append({**p, "price": price, "value": value, "pnl": pnl, "pct": pct})
        time.sleep(0.3)
    return result

# ─────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────
def main():
    print(f"\n{'='*50}")
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 開始抓取資料...")
    print(f"{'='*50}\n")

    results = []

    for stock in WATCH_LIST:
        code = stock["code"]
        print(f"► 處理 {stock['name']} ({code})")

        tech  = fetch_price_and_technicals(code)
        price = tech["price"]
        fund  = fetch_fundamental(code, price)
        chips = fetch_institutional(code, days=10)
        chips["margin_ratio"] = fetch_margin(code)

        fs = score_fundamental(fund)
        ts = score_technical(tech)
        cs = score_chips(chips)
        f_total = calc_layer(fs, SUB_WEIGHTS["fundamental"])
        t_total = calc_layer(ts, SUB_WEIGHTS["technical"])
        c_total = calc_layer(cs, SUB_WEIGHTS["chips"])
        total   = calc_total(f_total, t_total, c_total)

        print(f"  → 總分 {total}（基:{f_total} 技:{t_total} 碼:{c_total}）\n")

        results.append({
            **stock,
            "price":  price,
            "change": tech["change"],
            "scores": {"total": total, "fundamental": f_total, "technical": t_total, "chips": c_total},
            "fundamental": fund,
            "technical":   tech,
            "chips":       chips,
        })

        time.sleep(1)

    print("► 計算持股損益...")
    portfolio = fetch_portfolio()

    output = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "stocks":     results,
        "portfolio":  portfolio,
    }

    os.makedirs("data", exist_ok=True)
    with open("data/analysis.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    total_val = sum(p["value"] for p in portfolio)
    print(f"\n✅ 完成！持股總值 NT${total_val/10000:.1f}萬")
    print(f"   已輸出 data/analysis.json")

if __name__ == "__main__":
    main()
