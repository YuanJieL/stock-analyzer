"""
=============================================================
Railway 排程主程式
=============================================================
【說明】
Railway 部署後會執行此檔案
每天 16:30（台灣時間）自動呼叫 fetcher.main()
Railway 的環境變數 TZ 設為 Asia/Taipei

【免費額度】
Railway 免費方案每月 500 小時執行時間
此腳本每天執行約 5 分鐘，一個月約 150 分鐘，完全夠用
=============================================================
"""

from apscheduler.schedulers.blocking import BlockingScheduler
from fetcher import main as fetch_data
import os

scheduler = BlockingScheduler(timezone="Asia/Taipei")

# 每天週一到週五 16:30 執行（台股收盤後30分鐘）
@scheduler.scheduled_job("cron", day_of_week="mon-fri", hour=16, minute=30)
def daily_fetch():
    print("📊 開始每日資料抓取...")
    fetch_data()
    print("✅ 每日資料抓取完成")

if __name__ == "__main__":
    # 如果帶參數 --now 則立即執行一次（測試用）
    import sys
    if "--now" in sys.argv:
        print("🔧 手動執行模式")
        fetch_data()
    else:
        print("⏰ 排程模式啟動，等待每日 16:30 自動執行...")
        scheduler.start()
