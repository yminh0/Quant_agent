import os
import requests
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

def get_krx_market_data(target_date="20240502"):
    # 1. 유가증권시장에 상장되어 있는 주권의 매매정보 제공 API 엔드포인트 
    url = "https://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd"
    
    # 2. 명세서 기반 파라미터 (basDd 하나만 사용)
    params = {
        "basDd": target_date
    }
    
    # 3. 인증 헤더 (발급받은 키 적용)
    headers = {
        "AUTH_KEY": os.getenv("KRX_API_KEY")
    }

    print(f"{target_date} 전 종목 시세 데이터 요청 중...")

    try:
        response = requests.get(url, params=params, headers=headers)
        
        if response.status_code == 200:
            data = response.json()
            
            # 4. OutBlock_1 키에서 데이터 추출 
            if 'OutBlock_1' in data:
                df = pd.DataFrame(data['OutBlock_1'])
                
                # 5. 명세서 컬럼명에 맞춰 필요한 것만 추출 
                # TDD_CLSPRC(종가), TDD_OPNPRC(시가), ACC_TRDVOL(거래량) 등
                cols_map = {
                    'BAS_DD': 'date',
                    'ISU_CD': 'code',
                    'ISU_NM': 'name',
                    'TDD_OPNPRC': 'open',
                    'TDD_HGPRC': 'high',
                    'TDD_LWPRC': 'low',
                    'TDD_CLSPRC': 'close',
                    'ACC_TRDVOL': 'volume'
                }
                
                df = df[list(cols_map.keys())].rename(columns=cols_map)
                print(f"수집 성공! (총 {len(df)}개 종목)")
                return df
            else:
                print("OutBlock_1을 찾을 수 없습니다. 키 발급 상태를 확인하세요.")
                return None
        else:
            print(f"호출 실패: {response.status_code}")
            return None

    except Exception as e:
        print(f"에러: {e}")
        return None

if __name__ == "__main__":
    # 테스트로 특정 날짜 데이터 가져오기
    df_market = get_krx_market_data("20240430") 
    if df_market is not None:
        print(df_market.head())