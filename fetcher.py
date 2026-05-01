"""
=============================================================
台股智能分析系統 - 資料爬蟲 v4（FinMind版）
=============================================================

【v4 全面改用 FinMind API】
  不需要 Token，每小時300次請求免費額度完全夠用

  資料來源對應：
  股價/技術指標  → TaiwanStockPrice（日線OHLCV）
  P/E、P/B、殖利率 → TaiwanStockPER
  三大法人        → TaiwanStockInstitutionalInvestorsBuySell
  融資融券        → TaiwanStockMarginPurchaseShortSale
  財報EPS/ROE    → TaiwanStockFinancialStatements

  Base URL: https://api.finmindtrade.com/api/v4/data
=============================================================
"""

import json
import time
import requests
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

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
# 若有Token可填入（選填，無Token也能用）
FINMIND_TOKEN = ""

# ─────────────────────────────────────────────
# FinMind 統一請求函數
# ─────────────────────────────────────────────
# 【邏輯】
# 所有 FinMind 請求都走這個函數
# 參數：dataset名稱、股票代號、開始日期、結束日期
# 回傳：list of dict，失敗回傳空list

def finmind_get(dataset: str, stock_id: str,
                start_date: str, end_date: str = None) -> list:
    """統一的 FinMind API 請求"""
    if end_date is None:
        end_date = date.today().strftime("%Y-%m-%d")

    params = {
        "dataset":    dataset,
        "data_id":    stock_id,
        "start_date": start_date,
        "end_date":   end_date,
    }
    headers = {}
    if FINMIND_TOKEN:
        headers["Authorization"] = f"Bearer {FINMIND_TOKEN}"

    for attempt in range(3):
        try:
            res = requests.get(FINMIND_URL, params=params,
                               headers=headers, timeout=20)
            data = res.json()
            if data.get("status") == 200:
                return data.get("data", [])
            print(f"      FinMind {dataset} 回傳: {data.get('msg','')}")
            return []
        except Exception as e:
            print(f"      FinMind 請求失敗（第{attempt+1}次）: {e}")
            time.sleep(3)
    return []

# ─────────────────────────────────────────────
# Layer 2：技術面
# Dataset: TaiwanStockPrice
# 欄位: date, open, max, min, close, Trading_Volume
# ─────────────────────────────────────────────
# 【邏輯】
# 抓取近6個月日線資料
# 自行計算 MA20/MA60/KD/RSI/MACD
# 成交量與20日均量比較判斷放量/縮量

def fetch_price_and_technicals(code: str) -> dict:
    """從 FinMind 抓股價並計算技術指標"""
    start = (date.today() - timedelta(days=180)).strftime("%Y-%m-%d")
    rows  = finmind_get("TaiwanStockPrice", code, start)

    if not rows:
        print(f"      [技術面] {code} 無資料")
        return _empty_technical()

    df = pd.DataFrame(rows)
    df["date"]  = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    # 欄位對應
    close = df["close"].astype(float)
    high  = df["max"].astype(float)
    low   = df["min"].astype(float)
    vol   = df["Trading_Volume"].astype(float)

    price  = round(float(close.iloc[-1]), 2)
    prev   = float(close.iloc[-2]) if len(close) > 1 else price
    change = round((price - prev) / prev * 100, 2) if prev > 0 else 0

    # 均線
    ma20 = float(close.rolling(20).mean().iloc[-1])
    ma60 = float(close.rolling(60).mean().iloc[-1]) if len(close) >= 60 else ma20

    # 趨勢判斷
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

    # KD（9日）
    low9  = low.rolling(9).min()
    high9 = high.rolling(9).max()
    rsv   = ((close - low9) / (high9 - low9).replace(0, 1) * 100).fillna(50)
    k     = rsv.ewm(com=2, adjust=False).mean()
    d     = k.ewm(com=2, adjust=False).mean()
    kd_k  = round(float(k.iloc[-1]), 1)
    kd_d  = round(float(d.iloc[-1]), 1)

    # RSI（14日）
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rsi   = round(float((100 - 100 / (1 + gain / loss.replace(0, 0.001))).iloc[-1]), 1)

    # MACD（12,26,9）
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

    # 成交量
    vol_ma  = float(vol.rolling(20).mean().iloc[-1])
    today_v = float(vol.iloc[-1])
    vol_s   = ("大量" if today_v > vol_ma * 1.5 else
               "放量" if today_v > vol_ma * 1.1 else
               "縮量" if today_v < vol_ma * 0.7 else "平穩")

    support    = round(float(low.rolling(20).min().iloc[-1]), 2)
    resistance = round(float(high.rolling(20).max().iloc[-1]), 2)

    notes = []
    if macd_s == "黃金交叉": notes.append("MACD黃金交叉")
    if kd_k < 30: notes.append("KD超賣")
    if rsi < 35:  notes.append("RSI超賣")
    if trend == "上升": notes.append("均線多頭")

    print(f"      [技術面] 價格={price} 趨勢={trend} KD={kd_k} RSI={rsi} MACD={macd_s}")

    return {
        "price": price, "change": change,
        "ma20": ma20_s, "ma60": ma60_s, "trend": trend,
        "kd_k": kd_k, "kd_d": kd_d, "macd": macd_s,
        "rsi": rsi, "volume": vol_s,
        "support": support, "resistance": resistance,
        "ma20_val": round(ma20, 2), "ma60_val": round(ma60, 2),
        "note": "、".join(notes) if notes else "技術面中性",
    }

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
# Layer 1：基本面
# Dataset: TaiwanStockPER（P/E、P/B、殖利率）
#          TaiwanStockFinancialStatements（EPS、ROE）
# ─────────────────────────────────────────────
# 【邏輯】
# TaiwanStockPER：每日更新，直接取最新一筆
#   → dividend_yield（殖利率%）、PER（本益比）、PBR（淨值比）
#
# TaiwanStockFinancialStatements：季報
#   → 篩選 type == "EPS" 取得每股盈餘
#   → 篩選 type == "ROE" 取得股東權益報酬率
#   → EPS成長率 = (最新季EPS - 去年同季EPS) / 去年同季EPS * 100

def fetch_fundamental(code: str, price: float) -> dict:
    """從 FinMind 抓基本面資料"""
    # 1. P/E、P/B、殖利率
    start_per = (date.today() - timedelta(days=10)).strftime("%Y-%m-%d")
    per_rows  = finmind_get("TaiwanStockPER", code, start_per)

    pe, pb, dividend_yield = 0.0, 0.0, 0.0
    if per_rows:
        latest = per_rows[-1]
        pe = float(latest.get("PER") or 0)
        pb = float(latest.get("PBR") or 0)
        # FinMind 殖利率單位為 % 的100倍（如 213.5 = 2.135%），需除以100
        raw_dy = float(latest.get("dividend_yield") or 0)
        dividend_yield = round(raw_dy / 100, 2) if raw_dy > 100 else round(raw_dy, 2)

    # 2. 財報：EPS、ROE、負債比率
    start_fin = (date.today() - timedelta(days=400)).strftime("%Y-%m-%d")
    fin_rows  = finmind_get("TaiwanStockFinancialStatements", code, start_fin)

    eps, eps_growth, roe, revenue_growth = 0.0, 0.0, 0.0, 0.0

    if fin_rows:
        fin_df = pd.DataFrame(fin_rows)

        # EPS（FinMind type="EPS"，單位：元）
        eps_df = fin_df[fin_df["type"] == "EPS"].sort_values("date")
        if len(eps_df) >= 1:
            eps = float(eps_df["value"].iloc[-1])
        if len(eps_df) >= 5:
            # 與去年同季比較（前4季）
            eps_prev = float(eps_df["value"].iloc[-5])
            eps_growth = round((eps - eps_prev) / abs(eps_prev) * 100, 1) if eps_prev != 0 else 0

        # ROE = 本期淨利 / 股東權益（自行計算）
        # FinMind 沒有直接的 ROE 欄位
        # 用 IncomeAfterTaxes（本期淨利）/ EquityAttributableToOwnersOfParent（母公司淨利）近似
        net_df = fin_df[fin_df["type"] == "IncomeAfterTaxes"].sort_values("date")
        eq_df  = fin_df[fin_df["type"] == "EquityAttributableToOwnersOfParent"].sort_values("date")
        if len(net_df) >= 1 and len(eq_df) >= 1:
            net_income = float(net_df["value"].iloc[-1])
            equity     = float(eq_df["value"].iloc[-1])
            # 年化：單季淨利 × 4 / 股東權益
            roe = round((net_income * 4 / equity) * 100, 1) if equity > 0 else 0

        # 營收成長率 YoY（Revenue 欄位）
        rev_df = fin_df[fin_df["type"] == "Revenue"].sort_values("date")
        if len(rev_df) >= 5:
            rev_now  = float(rev_df["value"].iloc[-1])
            rev_prev = float(rev_df["value"].iloc[-5])
            revenue_growth = round((rev_now - rev_prev) / abs(rev_prev) * 100, 1) if rev_prev != 0 else 0

    # 3. 負債比率（資產負債表）
    debt_ratio = 0.0
    bal_rows = finmind_get("TaiwanStockBalanceSheet", code, start_fin)
    if bal_rows:
        bal_df = pd.DataFrame(bal_rows)
        # 正確欄位名稱：TotalAssets、TotalLiabilities
        asset_df = bal_df[bal_df["type"] == "TotalAssets"].sort_values("date")
        liab_df  = bal_df[bal_df["type"] == "TotalLiabilities"].sort_values("date")
        if len(asset_df) >= 1 and len(liab_df) >= 1:
            total_asset = float(asset_df["value"].iloc[-1])
            total_liab  = float(liab_df["value"].iloc[-1])
            debt_ratio  = round(total_liab / total_asset * 100, 1) if total_asset > 0 else 0

    # EPS 反推（若財報沒抓到但有 PE）
    if eps == 0 and pe > 0:
        eps = round(price / pe, 2)

    note = f"本益比{pe}倍，殖利率{dividend_yield}%，淨值比{pb}倍，EPS={eps}元"
    print(f"      [基本面] PE={pe} PB={pb} 殖利率={dividend_yield}% EPS={eps} ROE={roe}%")

    return {
        "eps": round(eps, 2),
        "eps_growth": eps_growth,
        "roe": roe,
        "pe": pe,
        "pb": pb,
        "dividend_yield": dividend_yield,
        "debt_ratio": debt_ratio,
        "revenue_growth": revenue_growth,
        "note": note,
    }

def _empty_fundamental():
    return {
        "eps": 0, "eps_growth": 0, "roe": 0,
        "pe": 0, "pb": 0, "dividend_yield": 0,
        "debt_ratio": 0, "revenue_growth": 0,
        "note": "基本面資料暫無法取得",
    }

# ─────────────────────────────────────────────
# Layer 3：籌碼面
# Dataset: TaiwanStockInstitutionalInvestorsBuySell
# 欄位: date, stock_id, name, buy, sell
# name 值: 外資, 投信, 自營商
# ─────────────────────────────────────────────
# 【邏輯】
# 抓近20個交易日的三大法人資料
# 計算外資/投信各自的「連續買超或賣超天數」
# 買超 = buy - sell > 0，以張（千股）為單位

def fetch_institutional(code: str, days: int = 15) -> dict:
    """從 FinMind 抓三大法人資料"""
    start = (date.today() - timedelta(days=days * 2)).strftime("%Y-%m-%d")
    rows  = finmind_get("TaiwanStockInstitutionalInvestorsBuySell", code, start)

    if not rows:
        print(f"      [籌碼] {code} 無資料")
        return _empty_chips()

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df["net"]  = df["buy"].astype(float) - df["sell"].astype(float)

    # 各法人分開處理
    def get_daily_net(name):
        sub = df[df["name"] == name].sort_values("date", ascending=False)
        return sub["net"].tolist()

    foreign_list = get_daily_net("外資")
    trust_list   = get_daily_net("投信")
    dealer_list  = get_daily_net("自營商")

    # 計算連續買超天數
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

    # 最新一日買賣超（單位：張，FinMind已是股數，除以1000換張）
    f_net = int(foreign_list[0] / 1000) if foreign_list else 0
    t_net = int(trust_list[0] / 1000) if trust_list else 0
    d_net = int(dealer_list[0] / 1000) if dealer_list else 0

    notes = []
    if f_days >= 3:  notes.append(f"外資連買{f_days}天")
    if t_days >= 2:  notes.append(f"投信連買{t_days}天")
    if f_days >= 3 and t_days >= 2: notes.append("法人同步買超")
    if f_days <= -3: notes.append(f"外資連賣{abs(f_days)}天⚠️")

    print(f"      [籌碼] 外資{f_days}天/{f_net}張 投信{t_days}天/{t_net}張 自營{d_net}張")

    return {
        "foreign_days": f_days,
        "foreign_net":  f_net,
        "trust_days":   t_days,
        "trust_net":    t_net,
        "dealer_net":   d_net,
        "big_holder_change": "N/A",
        "margin_ratio": fetch_margin(code),
        "is_warning":   False,
        "note":         "、".join(notes) if notes else "無明顯法人訊號",
    }

def _empty_chips():
    return {
        "foreign_days": 0, "foreign_net": 0,
        "trust_days": 0, "trust_net": 0,
        "dealer_net": 0,
        "big_holder_change": "N/A",
        "margin_ratio": "N/A",
        "is_warning": False,
        "note": "籌碼資料暫無法取得",
    }

# ─────────────────────────────────────────────
# 融資水位
# Dataset: TaiwanStockMarginPurchaseShortSale
# 欄位: MarginPurchaseTodayBalance（融資餘額）
#       MarginPurchaseLimit（融資限額）
# ─────────────────────────────────────────────
def fetch_margin(code: str) -> str:
    start = (date.today() - timedelta(days=5)).strftime("%Y-%m-%d")
    rows  = finmind_get("TaiwanStockMarginPurchaseShortSale", code, start)
    if not rows:
        return "N/A"
    latest  = rows[-1]
    balance = float(latest.get("MarginPurchaseTodayBalance") or 0)
    limit   = float(latest.get("MarginPurchaseLimit") or 1)
    ratio   = balance / limit if limit > 0 else 0
    return "低" if ratio < 0.15 else "中" if ratio < 0.25 else "高"

# ─────────────────────────────────────────────
# 評分引擎
# ─────────────────────────────────────────────
WEIGHTS = {"fundamental": 35, "technical": 35, "chips": 30}
SUB_WEIGHTS = {
    "fundamental": {"eps_growth":15,"roe":15,"pe":12,"pb":8,
                    "dividend_yield":12,"debt_ratio":10,"revenue_growth":15,"moat":13},
    "technical":   {"trend":20,"ma":15,"kd":15,"macd":15,
                    "rsi":10,"volume":15,"support":10},
    "chips":       {"foreign":35,"trust":20,"dealer":10,
                    "big_holder":20,"margin":15},
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
    return round(f * WEIGHTS["fundamental"]/100 +
                 t * WEIGHTS["technical"]/100 +
                 c * WEIGHTS["chips"]/100)

# ─────────────────────────────────────────────
# 持股損益
# ─────────────────────────────────────────────
def fetch_portfolio() -> list:
    result = []
    start  = (date.today() - timedelta(days=5)).strftime("%Y-%m-%d")

    for p in PORTFOLIO:
        rows  = finmind_get("TaiwanStockPrice", p["code"], start)
        price = p["cost"]  # 預設用成本價

        if rows:
            price = float(rows[-1]["close"])

        value = round(price * p["shares"], 0)
        cost  = p["cost"] * p["shares"]
        pnl   = round(value - cost, 0)
        pct   = round(pnl / cost * 100, 2) if cost > 0 else 0

        result.append({**p, "price": price, "value": value,
                       "pnl": pnl, "pct": pct})

        time.sleep(0.5)  # 避免請求過快

    return result

# ─────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────
def main():
    print(f"\n{'='*50}")
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 台股分析系統 v4 (FinMind) 啟動")
    print(f"{'='*50}\n")

    results = []

    for stock in WATCH_LIST:
        code = stock["code"]
        print(f"► 處理 {stock['name']} ({code})")

        tech  = fetch_price_and_technicals(code)
        price = tech["price"]
        fund  = fetch_fundamental(code, price)
        chips = fetch_institutional(code, days=20)

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
            "scores":      {"total": total, "fundamental": f_total,
                            "technical": t_total, "chips": c_total},
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
    print(f"\n{'='*50}")
    print(f"✅ 完成！持股總值 NT${total_val/10000:.1f}萬")
    print(f"   資料來源：FinMind API（無需Token）")
    print(f"   已輸出 data/analysis.json")
    print(f"{'='*50}\n")

if __name__ == "__main__":
    main()
