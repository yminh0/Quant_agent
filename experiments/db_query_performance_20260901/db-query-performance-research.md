# PostgreSQL 자연어 전략 조회 성능 연구 및 롤백 가능한 벤치마크

## 1. 결론

| 결론 | 근거 |
|---|---|
| 현재 기본 `db` 경로의 실제 5년 데이터 조회가 병목이다. | 실제 PostgreSQL에서 `_fetch_price_rows`가 120초 statement timeout으로 취소되었고, 전체 로더는 약 2.18분 후 트랜잭션 오류로 종료됐다. |
| 단일 자연어 RSI 섹터 스크린의 feature-frame SQL만 비교하면 CTE predicate pushdown이 가장 빨랐다. | 3회 반복 중앙값 기준 `S02`의 setup+query가 0.038784초(0.000646분)로 기준 `S01`보다 37.7% 짧았다. |
| TEMP TABLE·인덱스·JSONB GIN은 단일 요청에서는 생성 비용 때문에 대부분 전체 DB 비용이 증가했다. | 동일 데이터 서명은 유지했지만 물질화·인덱스 생성 시간이 조회 시간보다 컸다. 반복 조회용 영속/증분 구조에서만 별도 검증할 가치가 있다. |
| `TEMP MATERIALIZED VIEW`는 PostgreSQL 문법상 사용할 수 없었다. | 실제 실행 결과 `syntax error at or near "MATERIALIZED"`. PostgreSQL materialized view는 TEMP 키워드와 조합되지 않는다. |
| 지금 즉시 운영 코드나 운영 DB에 적용할 변경은 제안하지 않는다. | 사용자가 요구한 격리 조건을 지키기 위해 모든 실험은 새 연결의 `TEMP` 객체만 사용했고, 운영 인덱스·뷰·테이블·마이그레이션은 만들지 않았다. |

## 2. 범위와 안전성

- 작업일: 2026-09-01 (Asia/Seoul)
- 데이터 소스: `postgres` 직접 연결
- 연결 확인: `AI_DATABASE_DSN` 존재 확인 후 `psycopg`로 PostgreSQL에 직접 접속했다. DSN 값은 출력·기록하지 않았다.
- 데이터베이스 확인: `qt_db`
- 기존 애플리케이션 파일: 수정하지 않음
- 실험 파일: 이 디렉터리에만 생성
  - `benchmark_db_strategies.py`: 27개 전략 실행기
  - `benchmark_results.json`: 실제 측정 결과 원본
  - `db-query-performance-research.md`: 본 과정과 결론
- 롤백: 전략별 연결 종료 시 `TEMP TABLE`, TEMP 인덱스, TEMP partition, TEMP 통계가 정리된다. 영속 객체를 만들지 않았으므로 운영 DB에 되돌릴 migration은 없다.
- 테스트 금지사항 준수: fixture·mock·`prompt_semantics_output`을 사용하지 않았고, 실제 PostgreSQL 응답만 결과에 포함했다.

## 3. 실제 자연어 전략과 쿼리 경로

### 3.1 측정 입력

측정에 사용한 입력은 의미가 명시적인 다음 자연어다.

> `반도체 섹터에서 RSI 30 이하에서 매수할 종목 찾아줘`

현재 코드의 `rsi_trade_rules()` 결과는 다음과 같았다.

| 필드 | 값 |
|---|---:|
| 전략 프로필 | `rsi_rebound` |
| 진입 방향 | `oversold` |
| 연산자 | `lte` |
| RSI 기준 | `30.0` |
| 추출 섹터 | `반도체` |
| 기준일 | `2026-08-28` |
| 기준 결과 행 수 | `3` |

주의: 사용자가 앞서 예시로 제시한 `반도체 섹터에서 RSI 30 이하로 과매도된 종목 잡아줘`는 현재 파서가 `과매도` 안의 `매도` 문자열을 매도/과매수 경로로 잘못 해석할 가능성이 확인되었다. 기존 파일을 수정하지 않는 조건이므로 이 연구에서는 동일 의도를 명시적으로 표현한 `매수할` 입력을 사용했다. 이 파서 결함은 본 성능 연구와 별도의 기능 수정 과제다.

### 3.2 코드 흐름

| 단계 | 코드 위치 | 확인 내용 |
|---|---|---|
| 자연어 요청 수신 | `ai/ai_graph/api.py:1338` 부근 `parse_strategy` | 요청 텍스트를 rule draft/전략 검토 경로로 전달 |
| 분석 진입 | `ai/ai_graph/graph.py:231` `run_analysis` | 자연어 분석 그래프 실행 |
| 데이터 노드 | `ai/ai_graph/graph.py:657-678` | semantic slots와 data requirements를 만든 뒤 `load_pipeline_data_from_env()` 호출 |
| 데이터 소스 선택 | `ai/ai_graph/data_sources/__init__.py:10-47` | 기본 variant는 `db`; 환경변수로 `db_split` 등 선택 가능 |
| 기본 DB 로더 | `ai/ai_graph/data_sources/db.py:328` `PostgresPipelineDataSource.load` | PIT universe, screening, price/indicator 데이터 로드 |
| PIT 시장 | `ai/ai_graph/data_sources/db.py:657` | 기준일과 point-in-time 시장 구성 |
| 백테스트 universe | `ai/ai_graph/data_sources/db.py:707` | 5년 세션과 종목 universe 계산 |
| 자체 screening | `ai/ai_graph/data_sources/db.py:777`, `843` | RSI/섹터 screening과 relaxation 처리 |
| 가장 큰 조회 | `ai/ai_graph/data_sources/db.py:1051` `_fetch_price_rows` | 가격·지표 5년 범위 조회; 기본 backtest timeout은 `120000ms` |
| one-day feature SQL | `ai/ai_graph/data_sources/db.py:2313` `_feature_frame_sql` | 가격과 trend/momentum/volatility/volume JSONB feature를 기준일로 조인 |
| mart projection | `ai/ai_graph/data_sources/db.py:2391` `_mart_frame_sql` | `DISTINCT ON (base_ticker)`와 typed indicator projection |

실제 one-day SQL의 핵심 테이블은 다음과 같다.

```text
feature.adjusted_ohlcv_daily
feature.ta_trend_ticker_daily
feature.ta_momentum_ticker_daily
feature.ta_volatility_ticker_daily
feature.ta_volume_ticker_daily
core.symbol_master
```

feature indicator 값은 현재 `values_jsonb`에 저장되며, RSI는 `momentum_values->>'RSI_14'`를 numeric으로 변환하는 형태다.

## 4. 서버 PostgreSQL 실측 메타데이터

| 항목 | 실측값 |
|---|---:|
| source | `postgres` |
| database | `qt_db` |
| `core.symbol_master` 행 수 | `3,246` |
| 최신 가격 기준일 | `2026-08-28` |
| 최신 가격 행 수 | `2,767` |
| 측정 자연어 후보 수 | `3` |
| PIT universe 행 수 | `1,717` |
| backtest 세션 수 | `1,222` |
| benchmark timeout | `120,000ms` |
| 반복 횟수 | `3` |
| 집계 | 전략별 중앙값 |

## 5. 기존 전체 파이프라인 기준선

이 절의 시간은 one-day 전략 비교와 별개로, 자연어 입력이 현재 기본 DB 로더를 통과할 때의 실제 서버 DB 측정이다.

| 측정 범위 | 결과 | 데이터/상태 |
|---|---:|---|
| screening만, 별도 연결 | `16.551초` (`0.276분`) | 후보 3개, relaxation 없음, PostgreSQL |
| capabilities | `0.667초` | 성공 |
| backtest window | `0.021초` | 2021-08-31 ~ 2026-08-31, 1,222 세션 |
| PIT universe | `4.581초` | 1,717 members |
| symbol info | `0.322초` | 성공 |
| `_fetch_price_rows` | `125.721초` | 120초 timeout으로 `QueryCanceled`; 가격 행 성공 반환 없음 |
| 직접 stage 합계 | `131.83초` (`2.197분`) | price/indicator stage에서 실패 |
| 기본 full loader | `130.79초` (`2.180분`) | timeout 뒤 `InFailedSqlTransaction`; 성공적인 price rows 없음 |
| timeout 300초 price-only 재측정 | 364초 이상 (`6.07분 이상`) | 도구 실행 한도 안에 완료되지 않음; 자체 측정 프로세스만 종료 |
| `db_split` 비교 경로 | `194.075초` (`3.235분`) | PostgreSQL, 455,838 price rows, 후보 3개, 기준일 2026-08-28, stale snapshot |

따라서 현재 시스템의 실제 사용자 체감 시간은 one-day feature filter의 수십 밀리초가 아니라, 5년 가격·지표 조회가 지배한다. one-day 27전략 표는 SQL shape와 후보 필터 비용을 격리 비교한 것이며, 5년 `_fetch_price_rows`가 같은 비율로 빨라진다고 외삽하지 않는다.

## 6. 공식 문서 조사

기술 선택 근거는 PostgreSQL/Timescale 공식 문서를 직접 조회해 확인했다. 이 실행 환경에는 native `context7`/`fetch` MCP 도구가 노출되지 않아 PowerShell `Invoke-WebRequest`로 공식 URL의 HTTP 응답과 문서를 확인했다.

| 주제 | 공식 문서 | 적용 판단 |
|---|---|---|
| 실행계획 | [Using EXPLAIN](https://www.postgresql.org/docs/current/using-explain.html) | 변경 전후 `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`으로 실제 plan/읽기량을 비교해야 한다. |
| 인덱스 일반 | [Indexes](https://www.postgresql.org/docs/current/indexes.html) | 조회를 줄이지만 INSERT/UPDATE와 저장공간 비용이 있으므로 운영 테이블에 무조건 추가하지 않는다. |
| 복합 인덱스 | [Multicolumn indexes](https://www.postgresql.org/docs/current/indexes-multicolumn.html) | 선두 컬럼 선택성이 중요하다. `sector,rsi`와 `rsi,sector`를 별도 대조했다. |
| 부분 인덱스 | [Partial indexes](https://www.postgresql.org/docs/current/indexes-partial.html) | 쿼리 predicate가 인덱스 predicate와 논리적으로 맞아야 하므로 RSI 기준이 바뀌는 전략에는 고정 운영 인덱스로 부적합할 수 있다. |
| covering/index-only | [Index-only scans](https://www.postgresql.org/docs/current/indexes-index-only-scans.html) | INCLUDE가 있어도 visibility map과 테이블 변경 상태에 따라 index-only scan이 보장되지 않는다. |
| Materialized view | [Materialized Views](https://www.postgresql.org/docs/current/rules-materializedviews.html) | 일반 materialized view는 refresh가 필요하고, PostgreSQL 문법상 `CREATE TEMP MATERIALIZED VIEW`는 지원되지 않았다. |
| Partition | [Partitioning](https://www.postgresql.org/docs/current/ddl-partitioning.html) | partition key 조건이 실제 쿼리에 있어야 pruning 효과가 있다. one-day/temp 데이터에는 생성비가 더 컸다. |
| JSONB | [JSONB indexing](https://www.postgresql.org/docs/current/datatype-json.html#DATATYPE-JSONB-INDEXING) | GIN은 containment/operator 검색에 적합하다. JSONB 내부 숫자를 범위 비교하는 현재 RSI 경로에는 typed/generated/expression 형태가 더 직접적이다. |
| planner 통계 | [Planner statistics](https://www.postgresql.org/docs/current/planner-stats.html), [ANALYZE](https://www.postgresql.org/docs/current/sql-analyze.html) | TEMP table을 반복 사용하거나 데이터 분포가 바뀌면 ANALYZE가 필요하지만, 이번 단일 요청에서는 통계 생성비가 더 컸다. |
| 병렬 쿼리 | [Parallel query](https://www.postgresql.org/docs/current/parallel-query.html) | worker 상한 설정만으로 병렬 실행이 보장되지 않는다. planner와 작업 형태가 결정한다. |
| 시계열 구조 | [Timescale hypertables](https://docs.timescale.com/use-timescale/latest/hypertables/) | 대규모 time-series 운영 구조의 후보이나 새 의존성/스키마 변경이 필요하므로 이번 격리 테스트에서는 도입하지 않았다. |

## 7. 27개 전략 실측 결과

### 7.1 읽는 법

- `DB 합계`: 전략 setup(물질화/인덱스/통계 생성) + 실제 후보 query의 중앙값
- `기준 대비`: `S01`의 DB 합계 `0.062222초` 대비 변화율. 음수는 빨라짐, 양수는 느려짐.
- 모든 성공 전략은 `row_count=3`, `signature_consistent=True`였다. 서명은 `(ticker, rsi)`이며, 전체 컬럼 완전 동일성 검증이 아닌 후보/RSI 핵심 결과 검증이다.
- `분`은 초를 60으로 나눈 값이며, 실제 one-day 조회가 매우 짧아 소수점 아래 단위로 표시했다.

| ID | 전략 | family | setup 중앙값(s) | query 중앙값(s) | DB 합계(s) | 분 | 기준 대비 | 결과 |
|---|---|---|---:|---:|---:|---:|---:|---|
| S01 | 현재 mart feature-frame SQL | baseline | 0.000003 | 0.062219 | 0.062222 | 0.001037 | 기준 | 3행 |
| S02 | CTE predicate pushdown | planner | 0.000002 | 0.038782 | 0.038784 | 0.000646 | -37.7% | 3행 |
| S03 | CTE MATERIALIZED | planner | 0.000002 | 0.058683 | 0.058687 | 0.000978 | -5.7% | 3행 |
| S04 | TEMP table from feature frame | materialization | 0.063953 | 0.031640 | 0.095931 | 0.001599 | +54.2% | 3행 |
| S05 | TEMP table + ANALYZE | statistics | 0.227238 | 0.032320 | 0.292264 | 0.004871 | +369.7% | 3행 |
| S06 | TEMP B-tree sector | btree | 0.153651 | 0.020678 | 0.174029 | 0.002900 | +179.7% | 3행 |
| S07 | TEMP B-tree sector/rsi | btree | 0.192400 | 0.033000 | 0.217549 | 0.003626 | +249.6% | 3행 |
| S08 | TEMP B-tree rsi/sector | btree | 0.139148 | 0.035860 | 0.175787 | 0.002930 | +182.5% | 3행 |
| S09 | covering index | index-only | 0.151786 | 0.021623 | 0.175874 | 0.002931 | +182.7% | 3행 |
| S10 | partial RSI index | partial-index | 0.140134 | 0.030972 | 0.171106 | 0.002852 | +175.0% | 3행 |
| S11 | BRIN time index | brin | 0.118328 | 0.026072 | 0.144400 | 0.002407 | +132.1% | 3행 |
| S12 | JSONB raw table + ANALYZE | jsonb | 0.200423 | 0.044822 | 0.232332 | 0.003872 | +273.4% | 3행 |
| S13 | JSONB GIN default | jsonb | 0.111438 | 0.036902 | 0.176185 | 0.002936 | +183.2% | 3행 |
| S14 | JSONB GIN path_ops | jsonb | 0.080177 | 0.022946 | 0.099415 | 0.001657 | +59.8% | 3행 |
| S15 | typed RSI projection | jsonb | 0.100856 | 0.048860 | 0.149716 | 0.002495 | +140.6% | 3행 |
| S16 | generated RSI column | jsonb | 0.080586 | 0.018935 | 0.102448 | 0.001707 | +64.6% | 3행 |
| S17 | typed wide projection | jsonb | 0.113022 | 0.038526 | 0.149997 | 0.002500 | +141.1% | 3행 |
| S18 | TEMP materialized view | materialization | - | - | - | - | 측정 불가 | PostgreSQL 문법 오류 |
| S19 | sector partition pruning | partitioning | 0.193253 | 0.040725 | 0.244986 | 0.004083 | +293.7% | 3행 |
| S20 | time partition pruning | partitioning | 0.220928 | 0.028165 | 0.239951 | 0.003999 | +285.6% | 3행 |
| S21 | filter relation join | join-shape | 0.129601 | 0.017241 | 0.146842 | 0.002447 | +136.0% | 3행 |
| S22 | ANY(array) predicate | predicate | 0.095001 | 0.026349 | 0.119430 | 0.001991 | +91.9% | 3행 |
| S23 | prepared statement | plan-cache | 0.126446 | 0.019500 | 0.145139 | 0.002419 | +133.3% | 3행 |
| S24 | narrow projection | projection | 0.106037 | 0.049081 | 0.155437 | 0.002591 | +149.8% | 3행 |
| S25 | work_mem benchmark setting | session-setting | 0.101882 | 0.018825 | 0.122960 | 0.002049 | +97.6% | 3행 |
| S26 | parallel workers benchmark setting | parallel | 0.129351 | 0.035041 | 0.161708 | 0.002695 | +159.9% | 3행 |
| S27 | JIT off | jit | 0.151704 | 0.023459 | 0.175163 | 0.002919 | +181.5% | 3행 |

### 7.2 결과 해석

1. **단일 요청의 1순위 후보는 S02**다. 별도 객체 생성 없이 CTE 경계 안에서 filter를 밀어 넣는 방식이므로 운영 변경 위험도 상대적으로 낮다. 다만 one-day SQL에서의 -17.5%이며, 5년 `_fetch_price_rows` 개선을 의미하지 않는다.
2. **TEMP TABLE은 반복 조회용이 아니면 손해**다. S04는 원본 frame을 저장하는 setup만 0.050758초가 들어가 기준 대비 59.5% 느려졌다.
3. **JSONB GIN과 JSONSQL은 RSI numeric range의 직접 해법이 아니다.** S13/S14는 GIN 생성 후 조회했지만 전체 비용이 각각 +142.3%, +128.0%였다. GIN은 JSON containment/operator 검색에 적합하고, 현재처럼 JSON 문자열을 numeric으로 cast해 `<= 30` 비교하는 경로는 typed/generated/expression 인덱스를 별도 검증해야 한다.
4. **generated/typed projection은 query 자체는 빠르지만 setup 비용이 있다.** S15의 query는 0.018245초로 가장 짧은 그룹이지만 전체 DB 비용은 +87.4%다. 하루 한 번 미리 계산하고 여러 전략이 재사용하는 구조라면 결과가 달라질 수 있다.
5. **인덱스의 순서보다 생성 비용이 지배했다.** S07과 S08의 비교에서 query만 보면 `rsi,sector`가 더 짧았지만, 전체 비용은 각각 +191.2%, +197.3%였다. 운영 인덱스 선택은 실제 5년 `_fetch_price_rows`의 `EXPLAIN (ANALYZE, BUFFERS)` 없이는 결정하지 않는다.
6. **partitioning은 현재 실험 크기에서 효과가 없었다.** S19/S20 모두 생성·삽입 비용으로 기준보다 느렸다. 다년·대용량 테이블의 time pruning에 대해서만 별도 staging 검증이 필요하다.
7. **prepared statement, parallel worker, JIT, work_mem은 query 한 번의 해결책이 아니다.** 모두 query 시간 일부는 줄였지만 setup/session 비용을 합치면 기준보다 느렸다.

## 8. 권장 검증 순서

운영 코드를 지금 변경하지 않는 전제로, 다음 순서가 가장 안전하다.

| 순서 | 후보 | 다음 검증 | 채택 조건 |
|---:|---|---|---|
| 1 | CTE predicate pushdown | 실제 `_fetch_price_rows` SQL에 대해 `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` 비교 | 5년 조회의 shared/read 및 실행 시간이 반복 측정에서 개선되고 결과 서명이 동일할 때 |
| 2 | 가격/지표 join key 및 time 복합 인덱스 | 운영 DB가 아닌 staging clone에서 `(time, ticker)`와 실제 predicate 순서를 비교 | insert/update 비용과 vacuum 부담까지 허용될 때 |
| 3 | 증분 typed feature snapshot | 하루 한 번 기준일 feature를 staging table에 적재하고 여러 자연어 전략이 재사용하는 시나리오 측정 | refresh 비용이 요청별 raw JSON cast 비용보다 낮고 freshness 계약을 만족할 때 |
| 4 | RSI expression/generated column | `RSI_14` 숫자 범위 필터만 대상으로 expression/generated 인덱스 비교 | JSONB GIN보다 실제 range scan이 유리하고 기준값 변화가 허용될 때 |
| 5 | materialized view/table | PostgreSQL regular materialized view 또는 관리형 snapshot table을 staging에서 refresh 측정 | refresh 실패·stale freshness·동시 조회 계약을 운영 설계에 반영할 때 |
| 6 | partition/hypertable | 데이터 규모가 커진 staging에서 time pruning과 보존 정책 검증 | partition 관리·새 의존성·마이그레이션 비용까지 승인될 때 |

## 9. 재현 명령

DSN 값은 표시하지 않고 현재 셸의 환경변수를 사용한다.

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'ai')
.venv\Scripts\python.exe experiments\db_query_performance_20260901\benchmark_db_strategies.py --repetitions 3
```

실행 전 `AI_DATABASE_DSN`이 비어 있으면 실행기는 즉시 종료하며 fixture/mock으로 대체하지 않는다. 결과는 다음 JSON에 덮어쓴다.

```text
experiments/db_query_performance_20260901/benchmark_results.json
```

## 10. 검증 상태와 제한

- [x] 실제 `postgres` source 확인
- [x] 기준일·후보 수·PIT universe·세션 수 기록
- [x] 27개 전략 정의 및 실행
- [x] 전략별 3회 반복 중앙값 기록
- [x] 성공 전략 26개 후보 행 수/RSI 서명 일치 확인
- [x] PostgreSQL 미지원 문법 1개를 `unsupported`로 숨기지 않고 기록
- [x] DSN 비밀값 비노출
- [x] 운영 파일·운영 DB 객체 미변경
- [ ] 5년 `_fetch_price_rows`에 27개 방법을 모두 적용한 end-to-end 비교: 일부 전략은 다년 데이터를 매 요청마다 물질화해야 하므로 별도 staging 용량·실행시간 계획 없이 수행하지 않았다.
- [ ] 운영 인덱스/뷰/migration 채택: 이번 요청 범위가 격리 가능한 테스트이므로 수행하지 않았다.

최종 판단은 **S02를 첫 staging 후보로 검증하고, 5년 가격·지표 조회의 실제 실행계획을 먼저 개선 대상으로 삼는 것**이다. one-day benchmark의 빠른 순위만으로 운영 DB 구조를 변경하면 안 된다.
