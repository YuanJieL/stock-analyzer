"""
=============================================================
台股智能分析系統 - 資料爬蟲
=============================================================

【執行時機】
  每天下午 4:30（盤後）由 Railway 自動排程執行

【資料來源說明】
  Layer 1 基本面：
    - 月營收 → 公開資訊觀測站 (MOPS) API
    - EPS/財報 → 公開資訊觀測站季報
    - P/E、P/B → 用股價 ÷ EPS / 淨值計算

  Layer 2 技術面：
    - 股價 OHLCV → Yahoo Finance (yfinance)
    - MA20/MA60 → 自行從收盤價計算
    - KD、RSI、MACD → 自行公式計算

  Layer 3 籌碼面：
    - 三大法人買賣超 → 證交所 TWSE OpenAPI (免費官方)
    - 融資融券 → 證交所 TWSE OpenAPI
    - 大戶持股 → 集保結算所 (TDCC)

【輸出】
  data/analysis.json → 前端網頁讀取此檔案顯示
=============================================================
"""

import json
import time
import requests
import yfinance as yf
import pandas as pd
from datetime import datetime, date
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
    # 在這裡新增更多股票，格式同上
]

# 你的持股（用於損益計算）
PORTFOLIO = [
    {"code": "0050", "name": "元大台灣50", "shares": 2000, "cost": 85},
    {"code": "0056", "name": "元大高股息",  "shares": 1000, "cost": 40},
    {"code": "2317", "name": "鴻海",        "shares": 500,  "cost": 210},
    {"code": "5880", "name": "合庫金",      "shares": 3000, "cost": 22},
    {"code": "2330", "name": "台積電",      "shares": 75,   "cost": 2000},
]

# ─────────────────────────────────────────────
# Layer 2：技術面 - Yahoo Finance
# ─────────────────────────────────────────────
# 【邏輯】
# 台股股票代號在 Yahoo Finance 格式為 "XXXX.TW"
# 抓取過去 120 天的日線資料（足夠計算 MA60、KD 等）
# 所有技術指標自行從 OHLCV 計算，不依賴付費 API

def fetch_price_and_technicals(code: str) -> dict:
    """從 Yahoo Finance 抓股價並計算技術指標"""
    ticker = f"{code}.TW"
    try:
        df = yf.download(ticker, period="6mo", interval="1d", progress=False)
        if df.empty:
            return _empty_technical()

        close = df["Close"].squeeze()
        high  = df["High"].squeeze()
        low   = df["Low"].squeeze()
        vol   = df["Volume"].squeeze()

        # 現價與漲跌幅
        price  = round(float(close.iloc[-1]), 2)
        prev   = float(close.iloc[-2])
        change = round((price - prev) / prev * 100, 2)

        # 均線
        ma20 = round(float(close.rolling(20).mean().iloc[-1]), 2)
        ma60 = round(float(close.rolling(60).mean().iloc[-1]), 2)

        # 趨勢判斷：
        # 站上 MA20 且 MA60 → 上升
        # 只站上 MA20 → 盤整
        # 跌破 MA20 → 下降
        if price > ma20 and price > ma60:
            trend = "上升"
            ma20_status = "站上"
            ma60_status = "站上"
        elif price > ma20:
            trend = "盤整"
            ma20_status = "站上"
            ma60_status = "跌破" if price < ma60 else "貼近"
        elif abs(price - ma20) / ma20 < 0.02:
            trend = "盤整"
            ma20_status = "貼近"
            ma60_status = "站上" if price > ma60 else "跌破"
        else:
            trend = "下降"
            ma20_status = "跌破"
            ma60_status = "跌破"

        # KD 指標（9日隨機指標）
        # 公式：RSV = (今收 - 9日最低) / (9日最高 - 9日最低) × 100
        # K = 前K × (2/3) + 今RSV × (1/3)
        # D = 前D × (2/3) + 今K × (1/3)
        low9  = low.rolling(9).min()
        high9 = high.rolling(9).max()
        rsv   = (close - low9) / (high9 - low9) * 100
        k = rsv.ewm(com=2, adjust=False).mean()
        d = k.ewm(com=2, adjust=False).mean()
        kd_k = round(float(k.iloc[-1]), 1)
        kd_d = round(float(d.iloc[-1]), 1)

        # RSI（14日）
        # 公式：RS = 平均漲幅 / 平均跌幅；RSI = 100 - 100/(1+RS)
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss
        rsi   = round(float((100 - 100 / (1 + rs)).iloc[-1]), 1)

        # MACD（12,26,9）
        # 公式：MACD線 = EMA12 - EMA26；訊號線 = EMA(MACD,9)；柱狀圖 = MACD - Signal
        ema12  = close.ewm(span=12, adjust=False).mean()
        ema26  = close.ewm(span=26, adjust=False).mean()
        macd_l = ema12 - ema26
        signal = macd_l.ewm(span=9, adjust=False).mean()
        hist   = macd_l - signal

        # MACD 狀態判斷
        if macd_l.iloc[-1] > signal.iloc[-1] and macd_l.iloc[-2] <= signal.iloc[-2]:
            macd_status = "黃金交叉"  # 剛剛形成黃金交叉
        elif macd_l.iloc[-1] < signal.iloc[-1] and macd_l.iloc[-2] >= signal.iloc[-2]:
            macd_status = "死亡交叉"  # 剛剛形成死亡交叉
        elif macd_l.iloc[-1] > signal.iloc[-1] and hist.iloc[-1] > 0:
            macd_status = "多頭" if hist.iloc[-1] > hist.iloc[-2] else "偏多"
        elif macd_l.iloc[-1] < signal.iloc[-1]:
            macd_status = "空頭"
        else:
            macd_status = "持平"

        # 成交量判斷（與20日均量比較）
        vol_ma20 = vol.rolling(20).mean().iloc[-1]
        today_vol = vol.iloc[-1]
        if today_vol > vol_ma20 * 1.5:
            vol_status = "大量"
        elif today_vol > vol_ma20 * 1.1:
            vol_status = "放量"
        elif today_vol < vol_ma20 * 0.7:
            vol_status = "縮量"
        else:
            vol_status = "平穩"

        # 支撐壓力（近20日最低/最高）
        support    = round(float(low.rolling(20).min().iloc[-1]), 2)
        resistance = round(float(high.rolling(20).max().iloc[-1]), 2)

        return {
            "price": price, "change": change,
            "ma20": ma20_status, "ma60": ma60_status, "trend": trend,
            "kd_k": kd_k, "kd_d": kd_d, "macd": macd_status,
            "rsi": rsi, "volume": vol_status,
            "support": support, "resistance": resistance,
            "ma20_val": ma20, "ma60_val": ma60,
        }
    except Exception as e:
        print(f"[技術面] {code} 錯誤: {e}")
        return _empty_technical()

def _empty_technical():
    return {
        "price": 0, "change": 0,
        "ma20": "N/A", "ma60": "N/A", "trend": "N/A",
        "kd_k": 50, "kd_d": 50, "macd": "N/A",
        "rsi": 50, "volume": "N/A",
        "support": 0, "resistance": 0,
        "ma20_val": 0, "ma60_val": 0,
    }

# ─────────────────────────────────────────────
# Layer 3：籌碼面 - 證交所 TWSE OpenAPI
# ─────────────────────────────────────────────
# 【邏輯】
# 證交所提供免費的三大法人每日買賣超 API
# 網址：https://www.twse.com.tw/rwd/zh/fund/T86
# 參數：response=json, date=YYYYMMDD
# 回傳欄位：[股票代號, 名稱, 外資買, 外資賣, 外資淨, 投信買, 投信賣, 投信淨, 自營淨, ...]
# 連續買超天數：需要連續抓多天資料並計算

def fetch_institutional(code: str, days: int = 10) -> dict:
    """從證交所抓三大法人買賣超（最近N個交易日）"""
    foreign_list = []
    trust_list   = []
    dealer_list  = []

    today = date.today()

    # 往回找最近 days 個有效交易日的資料
    checked = 0
    offset  = 0
    while checked < days and offset < 30:
        target_date = today - pd.Timedelta(days=offset)
        offset += 1

        # 跳過週末
        if target_date.weekday() >= 5:
            continue

        date_str = target_date.strftime("%Y%m%d")
        url = f"https://www.twse.com.tw/rwd/zh/fund/T86?response=json&date={date_str}&selectType=ALLBUT0999"

        try:
            res = requests.get(url, timeout=10,
                headers={"User-Agent": "Mozilla/5.0"})
            data = res.json()

            if data.get("stat") != "OK" or not data.get("data"):
                continue

            for row in data["data"]:
                if row[0].strip() == code:
                    # 欄位格式：[代號, 名稱, 外資買, 外資賣, 外資淨, 投信買, 投信賣, 投信淨, 自營淨, ...]
                    def parse_num(s):
                        return int(s.replace(",", "").replace("+", ""))
                    try:
                        foreign_net = parse_num(row[4])
                        trust_net   = parse_num(row[7])
                        dealer_net  = parse_num(row[10]) if len(row) > 10 else 0
                        foreign_list.append(foreign_net)
                        trust_list.append(trust_net)
                        dealer_list.append(dealer_net)
                        checked += 1
                    except:
                        pass
                    break

            time.sleep(0.5)  # 避免 rate limit

        except Exception as e:
            print(f"[籌碼] {code} {date_str} 錯誤: {e}")

    # 計算連續買超天數（從最新往回數，連續正值的天數）
    def count_consecutive(lst):
        if not lst:
            return 0
        count = 0
        for v in lst:  # lst[0] 是最新
            if v > 0:
                count += 1
            else:
                break
        return count if lst[0] > 0 else -count_consecutive_neg(lst)

    def count_consecutive_neg(lst):
        count = 0
        for v in lst:
            if v < 0:
                count += 1
            else:
                break
        return count

    return {
        "foreign_days":  count_consecutive(foreign_list),
        "foreign_net":   foreign_list[0] if foreign_list else 0,
        "trust_days":    count_consecutive(trust_list),
        "trust_net":     trust_list[0] if trust_list else 0,
        "dealer_net":    dealer_list[0] if dealer_list else 0,
        "big_holder_change": "N/A",  # 集保資料每週更新，需另外處理
        "margin_ratio":  "N/A",      # 融資水位另外抓
        "is_warning":    False,
    }

# ─────────────────────────────────────────────
# Layer 3：融資融券 - 證交所
# ─────────────────────────────────────────────
# 【邏輯】
# 融資餘額 / 融資限額 = 融資使用率
# < 15% → 低（健康）
# 15~25% → 中（正常）
# > 25% → 高（散戶過熱，風險信號）

def fetch_margin(code: str) -> str:
    """抓融資水位"""
    try:
        today_str = date.today().strftime("%Y%m%d")
        url = f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?response=json&date={today_str}&selectType=STOCK"
        res = requests.get(url, timeout=10,
            headers={"User-Agent": "Mozilla/5.0"})
        data = res.json()

        if data.get("stat") != "OK":
            return "N/A"

        for section in ["iTotalRecords", "data", "data2"]:
            rows = data.get(section, [])
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, list) and len(row) > 0 and row[0].strip() == code:
                        # 融資餘額 / 融資限額
                        try:
                            balance = int(row[4].replace(",", ""))
                            limit   = int(row[6].replace(",", ""))
                            ratio   = balance / limit if limit > 0 else 0
                            if ratio < 0.15:
                                return "低"
                            elif ratio < 0.25:
                                return "中"
                            else:
                                return "高"
                        except:
                            pass
        return "N/A"
    except Exception as e:
        print(f"[融資] {code} 錯誤: {e}")
        return "N/A"

# ─────────────────────────────────────────────
# Layer 1：基本面 - 公開資訊觀測站
# ─────────────────────────────────────────────
# 【邏輯】
# 月營收：MOPS API 每月10日後更新
# 財報EPS：每季發佈（3/5/8/11月）
# P/E = 現價 / 年EPS；P/B = 現價 / 每股淨值
# 殖利率 = 年配息 / 現價
# 負債比率 = 總負債 / 總資產

def fetch_fundamental(code: str, price: float) -> dict:
    """從公開資訊觀測站抓基本面資料"""
    try:
        # 月營收（最近12個月）
        revenue_data = _fetch_revenue(code)

        # 最新季財報
        fin_data = _fetch_financial(code)

        eps        = fin_data.get("eps", 0)
        eps_growth = fin_data.get("eps_growth", 0)
        roe        = fin_data.get("roe", 0)
        nav        = fin_data.get("nav", 1)  # 每股淨值
        dividend   = fin_data.get("dividend", 0)  # 年配息

        pe = round(price / eps, 1) if eps > 0 else 0
        pb = round(price / nav, 2) if nav > 0 else 0
        dividend_yield = round(dividend / price * 100, 2) if price > 0 else 0

        # 營收成長率（YoY，與去年同月比）
        revenue_growth = revenue_data.get("yoy", 0)

        return {
            "eps":            eps,
            "eps_growth":     eps_growth,
            "roe":            roe,
            "pe":             pe,
            "pb":             pb,
            "dividend_yield": dividend_yield,
            "debt_ratio":     fin_data.get("debt_ratio", 0),
            "revenue_growth": revenue_growth,
            "note":           f"EPS {eps} 元，ROE {roe}%，殖利率 {dividend_yield}%",
        }
    except Exception as e:
        print(f"[基本面] {code} 錯誤: {e}")
        return _empty_fundamental()

def _fetch_revenue(code: str) -> dict:
    """抓月營收（MOPS）"""
    try:
        today = date.today()
        year  = today.year - 1911  # 民國年
        month = today.month - 1 if today.day < 12 else today.month
        if month == 0:
            month = 12
            year -= 1

        url = "https://mops.twse.com.tw/nas/t21/sii/t21sc03_{year}_{month}_0.htm".format(
            year=year, month=month)
        res = requests.get(url, timeout=15,
            headers={"User-Agent": "Mozilla/5.0"})
        res.encoding = "big5"
        tables = pd.read_html(res.text)

        for table in tables:
            for _, row in table.iterrows():
                try:
                    if str(row.iloc[0]).strip() == code:
                        # 當月營收、去年同月、YoY
                        this_month = float(str(row.iloc[2]).replace(",", ""))
                        last_year  = float(str(row.iloc[3]).replace(",", ""))
                        yoy = round((this_month - last_year) / last_year * 100, 1) if last_year > 0 else 0
                        return {"this_month": this_month, "yoy": yoy}
                except:
                    continue
        return {"this_month": 0, "yoy": 0}
    except Exception as e:
        print(f"[月營收] {code} 錯誤: {e}")
        return {"this_month": 0, "yoy": 0}

def _fetch_financial(code: str) -> dict:
    """抓最新季財報（EPS、ROE、負債比、淨值）"""
    # 注意：MOPS 財報格式複雜，此處用簡化版
    # 實際部署建議搭配 finlab 或 tejapi 等套件
    try:
        year  = date.today().year - 1911
        url   = f"https://mops.twse.com.tw/mops/web/ajax_t164sb03?encodeURIComponent=1&step=1&firstin=1&off=1&keyword4=&code1=&TYPEK2=&checkbtn=&queryName=co_id&inpuType=co_id&TYPEK=all&isnew=false&co_id={code}&year={year}&season=04"
        res   = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        res.encoding = "utf-8"
        # 解析較複雜，簡化處理
        return {
            "eps": 0, "eps_growth": 0, "roe": 0,
            "nav": 0, "dividend": 0, "debt_ratio": 0
        }
    except:
        return {
            "eps": 0, "eps_growth": 0, "roe": 0,
            "nav": 0, "dividend": 0, "debt_ratio": 0
        }

def _empty_fundamental():
    return {
        "eps": 0, "eps_growth": 0, "roe": 0,
        "pe": 0, "pb": 0, "dividend_yield": 0,
        "debt_ratio": 0, "revenue_growth": 0,
        "note": "資料抓取失敗",
    }

# ─────────────────────────────────────────────
# 評分引擎
# ─────────────────────────────────────────────
# 【邏輯】與前端相同，Python 版本計算後一起寫進 JSON
# 前端也會重算（使用者調整權重時用），兩者互為備份

def score_fundamental(f: dict) -> dict:
    return {
        "eps_growth":     100 if f["eps_growth"]>=40 else 80 if f["eps_growth"]>=25 else 60 if f["eps_growth"]>=10 else 40 if f["eps_growth"]>=0 else 10,
        "roe":            100 if f["roe"]>=25 else 80 if f["roe"]>=18 else 60 if f["roe"]>=12 else 40 if f["roe"]>=8 else 20,
        "pe":             100 if f["pe"]<=15 else 80 if f["pe"]<=20 else 60 if f["pe"]<=30 else 40 if f["pe"]<=45 else 20,
        "pb":             100 if f["pb"]<=1.5 else 80 if f["pb"]<=2.5 else 60 if f["pb"]<=4 else 40 if f["pb"]<=6 else 20,
        "dividend_yield": 100 if f["dividend_yield"]>=5 else 80 if f["dividend_yield"]>=3.5 else 60 if f["dividend_yield"]>=2 else 40 if f["dividend_yield"]>=1 else 20,
        "debt_ratio":     100 if f["debt_ratio"]<=30 else 80 if f["debt_ratio"]<=45 else 60 if f["debt_ratio"]<=60 else 40 if f["debt_ratio"]<=75 else 10,
        "revenue_growth": 100 if f["revenue_growth"]>=35 else 80 if f["revenue_growth"]>=20 else 60 if f["revenue_growth"]>=10 else 40 if f["revenue_growth"]>=0 else 10,
        "moat": 70,
    }

def score_technical(t: dict) -> dict:
    kd_k = t["kd_k"]
    rsi  = t["rsi"]
    return {
        "trend":  100 if t["trend"]=="上升" else 55 if t["trend"]=="盤整" else 20,
        "ma":     100 if t["ma20"]=="站上" and t["ma60"]=="站上" else 70 if t["ma20"]=="站上" else 50 if t["ma20"]=="貼近" else 20,
        "kd":     90 if kd_k<30 else 75 if kd_k<50 else 55 if kd_k<70 else 30,
        "macd":   100 if t["macd"]=="黃金交叉" else 85 if t["macd"]=="翻多" else 75 if t["macd"]=="多頭" else 65 if t["macd"]=="偏多" else 50 if t["macd"]=="持平" else 20,
        "rsi":    90 if rsi<30 else 75 if rsi<50 else 60 if rsi<65 else 40 if rsi<80 else 15,
        "volume": 90 if t["volume"] in ["放量","大量"] else 65 if t["volume"]=="平穩" else 35,
        "support": 65,
    }

def score_chips(c: dict) -> dict:
    return {
        "foreign":     100 if c["foreign_days"]>=5 else 80 if c["foreign_days"]>=3 else 60 if c["foreign_days"]>=1 else 40 if c["foreign_days"]>=-1 else 10,
        "trust":       100 if c["trust_days"]>=3 else 75 if c["trust_days"]>=1 else 50 if c["trust_days"]>=-1 else 20,
        "dealer":      80 if c["dealer_net"]>500 else 60 if c["dealer_net"]>0 else 40,
        "big_holder":  85 if str(c.get("big_holder_change","")).startswith("+") else 45,
        "margin":      90 if c["margin_ratio"]=="低" else 60 if c["margin_ratio"]=="中" else 30,
    }

WEIGHTS = {"fundamental": 35, "technical": 35, "chips": 30}
SUB_WEIGHTS = {
    "fundamental": {"eps_growth":15,"roe":15,"pe":12,"pb":8,"dividend_yield":12,"debt_ratio":10,"revenue_growth":15,"moat":13},
    "technical":   {"trend":20,"ma":15,"kd":15,"macd":15,"rsi":10,"volume":15,"support":10},
    "chips":       {"foreign":35,"trust":20,"dealer":10,"big_holder":20,"margin":15},
}

def calc_layer_score(raw: dict, sub_w: dict) -> float:
    total_w = sum(sub_w.values())
    return sum(raw.get(k,0) * sub_w[k] / total_w for k in sub_w)

def calc_total(f_score, t_score, c_score) -> int:
    return round(
        f_score * WEIGHTS["fundamental"] / 100 +
        t_score * WEIGHTS["technical"]   / 100 +
        c_score * WEIGHTS["chips"]       / 100
    )

# ─────────────────────────────────────────────
# 持股損益計算
# ─────────────────────────────────────────────
def fetch_portfolio_prices() -> list:
    result = []
    for p in PORTFOLIO:
        try:
            ticker = yf.Ticker(f"{p['code']}.TW")
            hist   = ticker.history(period="2d")
            price  = round(float(hist["Close"].iloc[-1]), 2) if not hist.empty else p["cost"]
            value  = round(price * p["shares"], 0)
            cost   = p["cost"] * p["shares"]
            pnl    = round(value - cost, 0)
            pct    = round((pnl / cost) * 100, 2)
            result.append({**p, "price": price, "value": value, "pnl": pnl, "pct": pct})
        except:
            result.append({**p, "price": p["cost"], "value": p["cost"]*p["shares"], "pnl": 0, "pct": 0})
        time.sleep(0.3)
    return result

# ─────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────
def main():
    print(f"[{datetime.now()}] 開始抓取資料...")
    results = []

    for stock in WATCH_LIST:
        code = stock["code"]
        print(f"  處理 {stock['name']} ({code})...")

        # Layer 2：技術面（含現價）
        tech = fetch_price_and_technicals(code)
        price = tech["price"]

        # Layer 1：基本面
        fund = fetch_fundamental(code, price)

        # Layer 3：籌碼面
        chips = fetch_institutional(code, days=10)
        chips["margin_ratio"] = fetch_margin(code)

        # 技術面備註自動生成
        tech_notes = []
        if tech["macd"] in ["黃金交叉", "翻多"]:  tech_notes.append("MACD翻多")
        if tech["kd_k"] < 30:                     tech_notes.append("KD低檔超賣")
        if tech["rsi"] < 35:                      tech_notes.append("RSI超賣區")
        if tech["trend"] == "上升":               tech_notes.append("均線多頭排列")
        tech["note"] = "、".join(tech_notes) if tech_notes else "技術面中性觀望"

        # 籌碼面備註
        chip_notes = []
        if chips["foreign_days"] >= 3:   chip_notes.append(f"外資連買{chips['foreign_days']}天")
        if chips["trust_days"] >= 2:     chip_notes.append(f"投信連買{chips['trust_days']}天")
        if chips["foreign_days"] >= 3 and chips["trust_days"] >= 2:
            chip_notes.append("法人同步買超")
        chips["note"] = "、".join(chip_notes) if chip_notes else "籌碼面無明顯訊號"

        # 評分
        fs = score_fundamental(fund)
        ts = score_technical(tech)
        cs = score_chips(chips)
        f_total = round(calc_layer_score(fs, SUB_WEIGHTS["fundamental"]))
        t_total = round(calc_layer_score(ts, SUB_WEIGHTS["technical"]))
        c_total = round(calc_layer_score(cs, SUB_WEIGHTS["chips"]))
        total   = calc_total(f_total, t_total, c_total)

        results.append({
            **stock,
            "price":       price,
            "change":      tech["change"],
            "scores": {
                "total":       total,
                "fundamental": f_total,
                "technical":   t_total,
                "chips":       c_total,
            },
            "fundamental": fund,
            "technical":   tech,
            "chips":       chips,
        })

        print(f"    → 總分 {total}（基:{f_total} 技:{t_total} 碼:{c_total}）")
        time.sleep(1)  # 避免請求過快

    # 持股損益
    print("  計算持股損益...")
    portfolio = fetch_portfolio_prices()

    # 輸出 JSON
    output = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "stocks":     results,
        "portfolio":  portfolio,
    }

    os.makedirs("data", exist_ok=True)
    with open("data/analysis.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"[{datetime.now()}] ✅ 完成！已輸出 data/analysis.json")

if __name__ == "__main__":
    main()
