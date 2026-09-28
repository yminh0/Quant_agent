import os
import json
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

APP_KEY = os.getenv("KIS_APP_KEY")
APP_SECRET = os.getenv("KIS_APP_SECRET")
URL_BASE = "https://openapivts.koreainvestment.com:29443"

def get_access_token():
    token_file = "token.dat"
    
    # 1. 기존 토큰 파일 확인 (재사용 로직)
    if os.path.exists(token_file):
        with open(token_file, "r") as f:
            token_data = json.load(f)
        
        # 파일 생성 후 12시간 이내면 그대로 반환 (한투 토큰은 24시간 유효)
        if (datetime.now().timestamp() - os.path.getmtime(token_file)) < 43200:
            return token_data.get("access_token")

    # 2. 토큰 신규 발급
    url = f"{URL_BASE}/oauth2/tokenP"
    payload = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "appsecret": APP_SECRET
    }
    
    res = requests.post(url, json=payload)
    if res.status_code == 200:
        token = res.json().get("access_token")
        with open(token_file, "w") as f:
            json.dump(res.json(), f)
        print("새 토큰 발급 완료")
        return token
    else:
        print(f"토큰 발급 실패: {res.text}")
        return None