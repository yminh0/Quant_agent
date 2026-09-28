# 전략 입력·백테스트 범위·후보 카드 오류 분석 및 수정 방안

> 작성일: 2026-09-03  
> 범위: 원인 분석과 수정 방안 문서화만 수행함. 애플리케이션 코드는 수정하지 않음.

## 1. 결론

현재 문제는 하나의 오류가 아니라 다음 세 가지 상태가 섞여서 발생한다.

1. **매수 또는 매도 조건이 하나라도 없으면 실행 가능한 전략이 아니다.**
2. 기본 `db` 데이터 소스는 현재 추천 후보 수만 제한하고, 백테스트용 과거 가격 데이터는 고정 PIT(point-in-time) 유니버스 전체를 먼저 읽는다.
3. FE는 후보 카드가 실제로 있는지보다 대화가 존재하는지를 기준으로 빈 워크스페이스 문구를 결정한다. 따라서 카드가 없어도 `전략 후보를 선택해 주세요`가 표시될 수 있다.

권장 수정 방향은 다음과 같다.

| 순서 | 권장 방안 | 핵심 효과 |
|---|---|---|
| 1 | 백엔드 전략 입력 접수 단계에서 매수·매도 조건을 모두 검증 | 잘못된 전략이 DB·LLM·백테스트까지 진행되지 않음 |
| 2 | 백테스트 유니버스를 설정 가능한 PIT 범위로 먼저 제한 | 전체 유니버스 가격 데이터 로딩 방지 |
| 3 | 실패 원인을 폐쇄형 코드로 분류하고 `safe_message`만 FE에 전달 | 로그별로 일관된 사용자 문구 표시, 내부 오류 노출 방지 |
| 4 | FE를 `status`와 `candidate_cards.length` 조합으로 렌더링 | 후보 카드가 없을 때 잘못된 선택 안내 제거 |
| 5 | 서버 PostgreSQL 기반 통합 테스트로 실제 행 수·데이터 소스·기준일 검증 | fixture/mock 결과를 실제 결과로 오인하는 문제 방지 |

---

## 2. 현재 전략 입력부터 백테스트까지의 흐름

현재 흐름은 다음과 같다.

```text
사용자 전략 입력
  → FE POST /analysis-jobs { query }
  → 백엔드가 job 생성 및 백그라운드 실행 예약
  → Supervisor
  → Ambiguity Classifier
  → Data
  → Research
  → BacktestCode
  → Backtest
  → Signal / Risk Manager / Report
  → API Envelope 생성
  → FE가 job polling 결과로 채팅·워크스페이스 렌더링
```

근거:

- `fe/src/api/quantAgentClient.ts:323-341`: 입력 query로 `/analysis-jobs`를 호출한다.
- `ai/ai_graph/api.py:1434-1499, 1632-1648`: 요청을 job으로 만들고 백그라운드 실행한다.
- `ai/ai_graph/graph.py:179-199, 216-257`: 데이터·리서치·백테스트·리포트 순서로 그래프가 진행된다.

### 매수·매도 조건 검증의 현재 위치

`ai/ai_graph/strategy_parser.py:163-177`의 `_is_complete_supported_parse`는 다음을 모두 만족해야 실행 가능하다고 판단한다.

- `entry_conditions` 존재
- `exit_conditions` 존재
- 지원하지 않는 조건 없음
- 추가 clarification 불필요

따라서 `MACD 데드크로스 발생 시 매도`처럼 매도 조건만 있는 입력은 실행 가능한 전략으로 확정되면 안 된다. 다만 이 검증이 사용자 요청 접수 전에 동기적으로 끝나는 것이 아니라 이후 파이프라인에서 수행될 수 있으므로, FE에서는 안내 없이 job이 실패한 것처럼 보일 수 있다.

### 권장 접수 게이트

전략이 job으로 예약되기 전에 백엔드에서 다음을 검사한다.

```text
매수 조건 없음 → 즉시 전략 입력 오류
매도 조건 없음 → 즉시 전략 입력 오류
둘 다 없음     → 즉시 전략 입력 오류
둘 다 있음     → 기존 파이프라인 진행
```

이 검사는 FE에만 두면 안 된다. FE를 우회한 요청도 동일하게 처리해야 하므로 백엔드가 최종 권위가 되어야 한다.

권장 API 동작은 현재 polling 계약을 최소 변경하는 방식으로 **터미널 상태의 실패/거부 envelope를 즉시 반환**하는 것이다. 이 경우 job을 만들더라도 Data·Research·Backtest 실행은 예약하지 않는다. API 계약을 변경할 수 있다면 HTTP 4xx와 동일한 오류 payload를 사용할 수 있으나, 현재 FE가 job polling을 전제로 하므로 별도 계약 변경 범위가 커진다.

조건 누락 오류에는 후보 카드를 생성하지 않고, clarification 질문도 만들지 않는다. 이 입력은 “후보 중 선택” 문제가 아니라 “전략 자체가 실행 조건을 만족하지 않음” 문제이기 때문이다.

---

## 3. DB 뷰와 백테스트 범위의 현재 동작

### 현재 동작은 “뷰의 모든 컬럼을 무조건 전부 읽음”은 아님

현재 기본 데이터 소스는 `AI_DATA_SOURCE_VARIANT`가 별도로 지정되지 않으면 `db`이다.

- `ai/ai_graph/data_sources/__init__.py:8-18`: 기본 variant가 `db`이다.
- `ai/ai_graph/data_sources/db.py:745-768`: 백테스트 기간에 속하는 PIT 공통주 유니버스에서 `DISTINCT symbol`을 읽는다.
- `ai/ai_graph/data_sources/db.py:1105-1127`: 가격 데이터는 필요한 컬럼만 명시하고, 선택 ticker와 백테스트 기간으로 제한한다.
- `ai/ai_graph/data_sources/db.py:2449-2470`: 기본 경로는 mart feature view를 그대로 읽지 않고 base table 기반 feature frame을 구성한다.

즉, `SELECT *`로 뷰의 모든 컬럼을 한 번에 가져오는 구조는 확인되지 않는다. 그러나 **PIT 유니버스의 종목 목록은 범위 제한 없이 읽고**, 이후 그 유니버스의 과거 가격 행을 로드할 수 있으므로 실제 데이터량이 커질 수 있다.

### 현재 `backtest_max_tickers`의 한계

`ai/ai_graph/data_sources/db.py:397-465`의 흐름은 다음과 같다.

1. 현재 화면 추천용 screening을 수행한다.
2. 독립적으로 PIT 백테스트 시장 데이터를 로드한다.
3. `backtest_max_tickers`를 적용해 추천 후보 목록만 제한한다.
4. historical PIT 유니버스 자체는 그대로 유지한다.

코드 주석도 현재 추천 후보는 presentation context일 뿐, historical PIT universe와 price load를 제한하지 않는다고 명시한다(`db.py:397-400, 461-465`). 따라서 지금의 설정값은 **백테스트 데이터 로딩 범위가 아니라 추천 카드 범위에 가까운 제한**이다.

### 범위 축소 권장안

#### 권장안: PIT 유니버스 상한을 가격 조회 전에 적용

백테스트 시작 시점에 다음 순서로 범위를 결정한다.

```text
전략의 명시 ticker 여부 확인
  → 백테스트 기간과 데이터 기준일 확정
  → 설정값으로 PIT 유니버스 상한 결정
  → point-in-time 조건으로 유니버스 축소
  → 필요한 지표 family만 선택
  → 축소된 ticker와 기간으로 가격·지표 조회
```

설정값은 코드 상수가 아니라 운영 설정 또는 환경변수로 둔다. 예시는 이름만 제시하며 실제 값은 운영 정책으로 결정한다.

- `AI_BACKTEST_UNIVERSE_MAX_TICKERS`: 백테스트 유니버스 상한
- `AI_BACKTEST_MAX_TICKERS`: 추천 후보 상한
- `AI_BACKTEST_MAX_PRICE_ROWS`: 가격 행 안전 상한

두 상한을 구분해야 한다. 추천 카드 수를 줄이는 것만으로는 가격 데이터 로딩량이 줄지 않는다.

#### 유니버스 결정 원칙

- 명시적인 종목이 있으면 해당 종목 중심으로 최소 범위를 사용한다.
- 종목이 없으면 현재 시점 추천 결과를 과거 유니버스에 그대로 사용하지 않는다.
- 과거 유니버스는 백테스트 기간의 `as_of_date` 기준으로 결정한다.
- 현재 추천 후보를 추가할 때도 PIT 적합성을 확인하고, 전체 상한을 초과하면 fail-closed로 중단한다.
- 현재 화면 screening은 카드 표시용과 백테스트 universe 결정을 분리한다.

현재 `db_split`의 “제한된 기본 유니버스와 추천 종목의 결합” 방식은 참고할 수 있지만, 기본 `db`에 그대로 전환하려면 유니버스 정책과 look-ahead 방지 조건을 먼저 확정해야 한다.

#### 성능 안전장치

응답 metadata에 다음을 포함하고, 상한 초과 시 조용히 전체 조회하지 않는다.

| metadata | 의미 |
|---|---|
| `pipeline_data_source.source` | 실제 데이터 소스. 운영 결과는 `postgres`여야 함 |
| `as_of` / 백테스트 기간 | 기준일과 조회 기간 |
| `backtest_universe_size` | PIT 유니버스 종목 수 |
| `screening_candidate_count` | 현재 screening 후보 수 |
| `price_row_count` | 실제 가격 행 수 |
| `required_metrics` | 이번 전략에 필요한 지표 목록 |
| `scope_limit` | 적용한 설정 상한 |

---

## 4. 로그별 FE 문구 계약

### 기본 원칙

- 원시 Python·DB·provider 예외 문구를 FE에 전달하지 않는다.
- 백엔드는 `category`, `subcause`, `failure_stage`, `owner`, `retryable`, `safe_message`, `evidence_refs`를 구조화한다.
- FE는 `failure_cause.safe_message`를 사용자 문구로 사용한다.
- 운영 로그에는 `debug_ref`와 `evidence_refs`를 남기되, DSN·호스트·SQL·provider response body는 노출하지 않는다.
- 로그 코드와 FE 문구는 일대일로 매핑하고, FE에서 원문 오류를 재분류하지 않는다.

현재 이 계약의 필드는 `ai/ai_graph/schemas.py:28-39, 49-108, 203-224`에 있고, 실패 envelope는 `ai/ai_graph/jobs.py:1718-1737`에서 `safe_message`를 user payload에 넣는다.

### 권장 로그·문구 매핑

| `category` | `subcause` 또는 새 세부 코드 | FE 표시 문구 | 카드/재시도 정책 |
|---|---|---|---|
| `semantic_failure` | `missing_entry_condition` | `매수 조건이 없습니다. 매수 조건을 입력해 주세요.` | 카드 없음, 후보 선택 없음 |
| `semantic_failure` | `missing_exit_condition` | `매도 조건이 없습니다. 매도 조건을 입력해 주세요.` | 카드 없음, 후보 선택 없음 |
| `semantic_failure` | `missing_entry_and_exit_condition` | `매수 조건과 매도 조건을 모두 입력해 주세요.` | 카드 없음, 후보 선택 없음 |
| `semantic_failure` | `contract_shape_error` | `전략 조건을 실행 가능한 형태로 해석하지 못했습니다. 매수·매도 조건을 다시 입력해 주세요.` | 카드 없음, 입력 수정 유도 |
| `data_gap` | `no_screening_matches` | `조건에 맞는 종목을 찾지 못했습니다. 조건을 완화해 다시 시도해 주세요.` | 카드 없음, 조건 완화 재시도 |
| `data_gap` | `no_price_rows` | `선정된 종목의 가격 데이터가 적재되어 있지 않아 백테스트를 진행할 수 없습니다.` | 카드 없음, 다른 종목·기간 유도 |
| `data_gap` | `empty_analysis_result` | `분석 결과를 생성하지 못했습니다. 조건을 조정해 다시 시도해 주세요.` | 카드 없음, 재시도 |
| `infrastructure_failure` | `db_lock_capacity_exhausted` | `데이터 조회가 서버 자원 한도에 걸려 중단했습니다. 잠시 후 다시 시도해 주세요.` | 카드 없음, 재시도 가능 |
| `infrastructure_failure` | `db_connection_unavailable` | `운영 데이터 소스에 연결할 수 없습니다. 잠시 후 다시 시도해 주세요.` | 카드 없음, 재시도 가능 |
| `infrastructure_failure` | `aoai_capacity_exhausted` | `현재 AI 분석 요청이 몰려 대기 시간이 초과되었습니다. 잠시 후 다시 시도해 주세요.` | 카드 없음, 재시도 가능 |
| `infrastructure_failure` | `aoai_response_timeout` | `AI 응답이 제한 시간 안에 도착하지 않았습니다. 잠시 후 다시 시도해 주세요.` | 카드 없음, 재시도 가능 |
| `infrastructure_failure` | `aoai_connection_error` | `AI 제공자 연결에 일시적인 문제가 발생했습니다. 잠시 후 다시 시도해 주세요.` | 카드 없음, 재시도 가능 |
| `infrastructure_failure` | `aoai_http_4xx` / `aoai_http_5xx` | `AI 제공자 응답을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.` | 카드 없음, `retryable`에 따라 재시도 |
| `data_gap` | `fixture_mode_forbidden_in_release` | `운영 분석에는 검증 가능한 데이터 소스가 필요합니다. 데이터 소스가 준비된 뒤 다시 시도해 주세요.` | 카드 없음, 운영 데이터 연결 확인 |
| `cancelled` | `user_cancelled` | `분석을 취소했습니다.` | 후보 선택 없음 |

`missing_*` 코드는 현재 `FailureSubcause`에 없으므로 다음 중 하나를 선택해야 한다.

1. **권장:** 폐쇄형 세부 코드를 추가해 매수 누락·매도 누락·양쪽 누락을 구분한다.
2. 기존 `semantic_failure`/`clarification_failure` 코드를 재사용한다.

권장안은 사용자가 무엇을 고쳐야 하는지 바로 알 수 있고, 로그 집계도 가능하다는 이유로 1번이다. 어떤 경우에도 누락 조건을 일반 `unknown_failure`로 보내면 안 된다.

현재 구현에는 이미 `no_screening_matches`, `no_price_rows`, `empty_analysis_result`, provider 오류, DB 오류에 대한 안전 문구가 있다(`ai/ai_graph/jobs.py:1342-1605`). 새로 보강할 핵심은 **조건 누락을 백테스트 실패가 아닌 접수 단계의 명시적 전략 오류로 분류하는 것**이다.

---

## 5. 후보 카드가 안 보이는 원인과 FE 수정 방안

### 원인 A: FE가 후보 카드를 특정 상태에서만 노출함

`fe/src/api/quantAgentClient.ts:609-652`에서 후보 카드는 다음 조건일 때만 채팅 메시지에 전달된다.

```text
status === "need_clarification"
```

따라서 API payload에 후보 카드가 들어 있어도 `failed`, `rejected`, `ready` 상태에서는 FE 채팅 카드로 노출되지 않는다.

### 원인 B: 런타임 실패 envelope는 카드를 빈 배열로 만듦

`ai/ai_graph/jobs.py:1725-1736`의 실패 envelope는 `strategy_spec`을 비우고 기본 `UserPayload`를 생성한다. 관련 계약 테스트도 실패 결과의 `candidate_cards == []`를 확인한다(`ai/tests/test_screening_pipeline_failure_classifier.py:198-214`).

따라서 매수·매도 조건 누락이 파이프라인 진행 후 예외로 끝나면 후보 카드가 없는 것이 정상적인 실패 payload가 된다. 이 상태에서 후보 선택 문구를 표시하는 것이 FE 버그다.

### 원인 C: 카드 factory에 MACD 전용 분기가 없음

`ai/ai_graph/graph.py:1807-2134`의 정적 카드 factory에는 여러 전략 유형 분기가 있으나 MACD 전용 분기는 확인되지 않는다. 다만 기본 분기가 있으므로 이것만으로 “MACD라서 카드가 반드시 0개”라고 단정하면 안 된다. 실제 카드 수는 `strategy_candidate_cards`에 전달된 screening 후보 수와 최종 envelope 상태를 함께 확인해야 한다(`graph.py:801-824`).

### 원인 D: 빈 워크스페이스 문구가 카드 수를 검사하지 않음

`fe/src/pages/AppPage.tsx:242-249`는 `hasConversation`만 true이면 카드가 없어도 다음 문구를 표시한다.

> 전략 후보를 선택해 주세요

이것이 “후보 카드가 없는데도 선택해 달라”는 현상의 직접 원인이다.

### FE 상태 렌더링 규칙

후보 카드와 워크스페이스 문구는 다음 결정표를 기준으로 분리한다.

| 상태 | 카드 수 | FE 표시 | 워크스페이스 |
|---|---:|---|---|
| `running` | 무관 | 진행 중 문구 | 진행 상태 |
| `need_clarification` | 1 이상 | 후보 카드와 질문 표시 | 후보 선택 안내 가능 |
| `need_clarification` | 0 | 질문 또는 조건 보강 문구만 표시 | 후보 선택 문구 금지 |
| `rejected` | 0 | 오류/입력 수정 문구 | 오류 상태 또는 초기 상태 |
| `failed` | 0 | `failure_cause.safe_message` | 오류 상태 |
| `ready` | 무관 | 결과·리포트 표시 | 워크스페이스 표시 |

구현 시 핵심 조건은 다음과 같다.

```text
showCandidateCards = status == need_clarification && candidate_cards.length > 0
showCandidatePrompt = showCandidateCards
showFailureMessage = status == failed || 명시적 validation/rejected 오류
showEmptyWorkspaceSelection = showCandidateCards
```

즉, `analysisJobs.length > 0` 또는 `hasConversation`만으로 후보 선택 문구를 결정하면 안 된다. `latestJob.result.status`, `user_payload.candidate_cards.length`, `failure_cause`를 함께 사용해야 한다.

조건 누락 오류가 발생한 경우에는 다음을 보장한다.

- `candidate_cards: []`
- `clarification: undefined` 또는 조건 수정용 안내만 제공
- “전략 후보를 선택해 주세요” 미표시
- FE에 `safe_message`와 재입력 방향 표시

---

## 6. 수정 작업 순서

| 단계 | 담당 영역 | 변경 내용 | 완료 기준 |
|---|---|---|---|
| 1 | Backend admission | 매수·매도 조건 동시 존재 여부를 job 예약 전 검증 | 누락 시 DB·LLM·백테스트 호출 0회 |
| 2 | Backend error contract | 조건 누락 세부 코드와 safe message 추가 | 동일 코드가 로그·API·FE에서 유지 |
| 3 | Backend scope planner | 가격 조회 전에 PIT 유니버스 상한 적용 | 설정 상한 초과 조회 불가 |
| 4 | Backend metadata | source, as_of, universe size, price rows 기록 | 실제 실행 범위를 응답에서 확인 가능 |
| 5 | FE message mapping | `failure_cause.safe_message` 표시 | raw exception 미노출 |
| 6 | FE state matrix | status와 카드 수로 카드·빈 상태 분기 | 카드 0개일 때 후보 선택 문구 없음 |
| 7 | FE/Backend integration | 입력 누락·카드 0개·카드 존재·ready 상태 검증 | 상태별 화면이 결정표와 일치 |

---

## 7. 검증 시나리오와 합격 기준

### 전략 조건

| 시나리오 | 기대 결과 |
|---|---|
| 매수만 입력 | 즉시 조건 누락 오류, DB 호출 없음, 카드 없음 |
| 매도만 입력 | 즉시 조건 누락 오류, DB 호출 없음, 카드 없음 |
| 매수·매도 모두 없음 | 즉시 조건 누락 오류, DB 호출 없음, 카드 없음 |
| 매수·매도 모두 존재 | 기존 해석·데이터·백테스트 흐름 진행 |

### 후보 카드·FE 상태

| 시나리오 | 기대 결과 |
|---|---|
| `failed` + 카드 0개 | 오류 문구만 표시, 후보 선택 문구 없음 |
| `rejected` + 카드 0개 | 입력 수정 문구 표시, 후보 선택 문구 없음 |
| `need_clarification` + 카드 0개 | 질문/조건 보강 문구만 표시 |
| `need_clarification` + 카드 1개 이상 | 카드 표시 및 선택 가능 |
| `ready` | 결과 워크스페이스 표시, 후보 선택 문구 없음 |

### 백테스트 범위

- 실행 결과의 `pipeline_data_source.source`가 `postgres`인지 확인한다.
- 응답의 `as_of`, 백테스트 기간, 유니버스 종목 수, 가격 행 수를 확인한다.
- 실제 조회 ticker 수가 설정 상한을 넘지 않는지 확인한다.
- 기간 필터와 필요한 컬럼만 적용되는지 확인한다.
- 현재 screening 후보가 과거 기간에 look-ahead로 유입되지 않는지 확인한다.
- 후보 0개가 발생하면 `no_screening_matches`, 데이터 미연결, 기간 데이터 부족을 구분한다.
- fixture, mock, 캐시, `prompt_semantics_output` 결과는 운영 결과 검증에 사용하지 않는다.

---

## 8. 남은 결정 사항

1. 조건 누락을 HTTP 4xx로 즉시 반환할지, 현재 polling 호환성을 위해 터미널 `failed/rejected` job으로 반환할지 결정해야 한다.
2. 백테스트 유니버스 상한의 운영 설정 이름과 값은 성능 측정 후 정해야 한다. 코드에 고정 숫자를 넣지 않는다.
3. 기본 `db`와 `db_split` 중 어떤 유니버스 정책을 운영 표준으로 삼을지 결정해야 한다.
4. 현재 추천 카드와 과거 백테스트 universe를 분리할지, PIT 적합성이 검증된 후보만 제한적으로 결합할지 결정해야 한다.

## 9. 최종 정리

- `MACD 데드크로스 발생 시 매도`는 매수 조건이 없으므로 후보 선택 단계로 보내지 말고 접수 단계에서 오류 처리해야 한다.
- 현재 기본 `db` 경로는 뷰의 모든 컬럼을 무조건 읽는 구조는 아니지만, PIT 유니버스 전체 종목을 먼저 결정하고 과거 가격을 넓게 로드할 수 있다.
- `backtest_max_tickers`만 줄여서는 백테스트 범위가 충분히 줄지 않는다. 가격 조회 전에 별도 PIT 유니버스 상한을 적용해야 한다.
- 후보 카드 미표시는 API 상태가 `need_clarification`이 아니거나 실패 envelope의 카드가 빈 배열이기 때문일 수 있다.
- 후보 카드가 없는데도 선택 문구가 나오는 직접 원인은 `AppPage`가 실제 카드 수가 아니라 대화 존재 여부만 검사하는 것이다.
- 오류 문구는 raw log가 아니라 구조화된 `failure_cause.safe_message`를 기준으로 FE에 표시해야 한다.
