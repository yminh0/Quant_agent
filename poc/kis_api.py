import os
import requests
from poc.kis_auth import get_access_token
from datetime import datetime, timedelta

APP_KEY = os.getenv("KIS_APP_KEY")
APP_SECRET = os.getenv("KIS_APP_SECRET")
URL_BASE = "https://openapivts.koreainvestment.com:29443"

def get_current_price(code="005930"):
    token = get_access_token()
    if not token: return None

    url = f"{URL_BASE}/uapi/domestic-stock/v1/quotations/inquire-price"
    headers = {
        "Content-Type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST01010100"
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": code
    }

    res = requests.get(url, headers=headers, params=params)
    return res.json().get('output')

def get_daily_price(code="005930"):
    token = get_access_token()
    if not token: return None

    url = f"{URL_BASE}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
    
    # 날짜 설정 (오늘부터 40일 전까지)
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=40)).strftime("%Y%m%d")

    headers = {
        "Content-Type": "application/json",
        "authorization": f"Bearer {token}",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET,
        "tr_id": "FHKST03010100"
    }
    
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": code,
        "FID_INPUT_DATE_1": start_date,  # 시작일자 (YYYYMMDD)
        "FID_INPUT_DATE_2": end_date,    # 종료일자 (YYYYMMDD)
        "FID_PERIOD_DIV_CODE": "D",      # D:일봉
        "FID_ORG_ADJ_PRC": "0"
    }

    res = requests.get(url, headers=headers, params=params)
    data = res.json()
        
    return data.get('output2')

if __name__ == "__main__":
    # 1. 당일 현재가 테스트
    print("현재가 조회")
    curr_data = get_current_price("005930")
    
    if curr_data:
        print(f"삼성전자 현재가: {curr_data['stck_prpr']}원")
        print(f"전일대비: {curr_data['prdy_vrss']}원 ({curr_data['prdy_ctrt']}%)")
        print("-" * 40)

    # 2. 최근 30일 시세 테스트
    print("최근 일봉 데이터 조회")
    daily_data = get_daily_price("005930")
    
    if daily_data:
        print(f"--- 최근 30거래일 시세 (최신순) ---")
        for day in daily_data[:30]:
            print(f"날짜: {day['stck_bsop_date']} | 종가: {day['stck_clpr']} | 거래량: {day['acml_vol']}")


