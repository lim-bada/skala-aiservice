<!-- [추가] 조건부 워크플로 저장·미리보기·실행 API의 신규 사용 설명서 전체. -->
# 워크플로 API 사용법

작성일: 2026-10-21. 코드 대조일: 2026-10-22. 기본 백엔드 주소는 `http://127.0.0.1:8100`이다.

프로젝트 루트에서 fake 백엔드로 시작한다.

```bash
LLM_BACKEND=fake ./.venv/bin/python3 server.py
```

fake는 등록된 예시 요청만 처리한다. 실제 자연어 정확도는 OpenAI로 별도 평가한다. 웹 서버를 `./.venv/bin/python3 web_demo_server.py`로 실행하면 8600 포트에서 같은 API를 중계한다. [화면 사용법·코드 역할](workflow_ui.md)을 참고한다.

## 요청 순서

| 메서드·경로 | 입력·결과 |
|---|---|
| `GET /workflows/catalog` | 시스템·런북·조건 필드·제한 조회 |
| `POST /workflows/generate` | `{"query":"자연어 요청"}` → 검증된 초안 생성·저장, HTTP 201 |
| `GET /workflows/{workflow_id}` | 원문·정의·해시·표시 이름·상태 조회 |
| `POST /workflows/{workflow_id}/save` | `{"name":"표시 이름"}` → 재사용 목록에 등록 |
| `GET /workflows` | 저장한 절차 목록. `limit=20`, `offset=0`, `include_drafts=false` 기본 |
| `POST /workflows/{workflow_id}/execute` | `{"definition_hash":"미리보기의 해시"}` → 조회·보고 결과 |
| `POST /workflows/{workflow_id}/execute/stream` | 같은 실행을 POST SSE로 수신 |

생성·미리보기·저장은 MCP를 호출하지 않는다. 생성 응답에는 `workflow_id`, `name`, `query`, `definition`, `definition_hash`, `status`, 시각과 `compilation`이 들어간다. `compilation`에는 수정 생성까지 포함한 시도·토큰 사용량이 있다.

<!-- [추가] 사전 지원 범위 검사와 생성 후보의 제한된 의도 검사를 설명한다. -->
명백한 운영 변경·반복·미지원 조회 또는 대상·기준이 불명확한 요청은 생성 모델을 호출하기 전에 `UNSUPPORTED_REQUEST`로 거절할 수 있다. 이 경우 `attempts=[]`, 사용량은 0이다. 구조 검증을 통과한 후보도 명시한 순서·대상·조건과 어긋나면 `INTENT_MISMATCH` 피드백으로 한 번 수정한다. 끝까지 실패하면 정의를 저장하지 않는다. 이 검사는 정해진 어휘의 명시적 제약 검사이며 범용 자연어 해석 보장이 아니다. 기존 저장 정의를 자동 재작성하지 않는다.

<!-- [추가] 초안도 즉시 파일 DB에 남으며 저장 목록이 실행 이력을 의미하지 않음을 구분한다. -->
새 절차는 `draft` 상태로 DB에 즉시 기록된다. 저장 버튼은 이를 `saved`로 바꾸어 기본 목록에 표시한다. `include_drafts=true`로 초안도 조회할 수 있다. 페이지 크기는 1~100이며 생성 시각·ID의 내림차순이다. 저장 목록은 절차 정의 목록이고 실행 결과·진행 이벤트의 영구 이력은 아니다.

## Python 실행 예시

서버를 실행한 상태에서 별도 터미널의 프로젝트 루트에서 실행한다.

```python
import httpx

query = "payments-service를 조회하고 CPU가 80% 이상이면 CPU 런북을, 아니면 메모리 런북을 확인해줘."

with httpx.Client(base_url="http://127.0.0.1:8100", timeout=130) as client:
    response = client.post("/workflows/generate", json={"query": query})
    response.raise_for_status()
    draft = response.json()
    path = f"/workflows/{draft['workflow_id']}"

    response = client.get(path)
    response.raise_for_status()
    preview = response.json()
    print(preview["definition"])

    response = client.post(path + "/save", json={"name": "결제 CPU 조건부 점검"})
    response.raise_for_status()

    response = client.post(path + "/execute", json={
        "definition_hash": preview["definition_hash"],
    })
    response.raise_for_status()
    result = response.json()
    print(result["run_id"], result["report_valid"])
    print(result["final"])
```

<!-- [추가] 코드 생성 사실과 모델 요약·실패 복원의 공개 결과 계약이다. -->
## 사실 보고와 런북 요약

`final`은 코드가 출력한 관측값·조건·조회 횟수와 검증을 통과한 런북 요약·근거 원문을 합친 보고다. 런북을 읽지 않은 경로는 **실행 후 보고 모델** 호출 0회, `report_attempts=[]`, 보고 토큰 합계 0이다. 새 절차를 생성할 때의 모델 호출·사용량은 별도 `compilation`에 기록한다.

조회에 성공하면 `facts_report`에 모델 요약이 없는 사실 보고도 반환한다. 이후 요약이 실패해도 이 필드는 유지하며, 실패 상태·오류와 `report_valid=false`, 빈 `final`을 유지한다. 화면에서 확인된 조회 결과를 별도로 볼 수 있다. 조회 자체가 실패하면 `facts_report`는 비어 있다. [구조화 요약 계약·검증 한계](workflow_report_accuracy.md)를 참고한다.

## 저장과 실행 일치

`workflow.db`는 프로젝트 루트에 생기며 기존 `session.db`와 분리된다. 파일 DB를 다시 열면 저장한 절차가 유지된다. DB와 WAL·SHM 등 보조 파일은 소스 제외 목록에 등록했다.

실행 JSON은 생성 이후 변경하지 않는다. 저장 API는 표시 이름인 `name`과 상태·저장 시각만 갱신한다. `definition.name`은 생성 당시 이름을 보존하며 화면의 표시 이름은 최상위 `name`을 사용한다. 이름을 바꿔도 실행 해시는 같다.

해시는 `schema_version`과 `steps`의 정규화 JSON으로 계산한다. 실행 요청은 저장된 해시와 미리보기 해시가 같아야 한다. 현재 카탈로그에서 전체 정의를 다시 검사하고, 통과한 스냅샷만 실행한다. 실행할 때 생성 모델을 다시 호출하거나 사용자의 요청 body에서 단계를 받지 않는다. 같은 저장 절차를 다시 실행하면 새 실행 ID를 얻고, 정의에 모니터링 단계가 있다면 값을 새로 조회한다.

SQLite 작업과 카탈로그 파일 읽기는 `asyncio.to_thread`로 수행한다. 저장소는 스레드 잠금·트랜잭션·WAL을 사용하고, 읽을 때마다 정의 JSON·스키마 버전·해시를 검사한다.

## POST 스트림

`execute/stream`도 JSON 실행과 같은 body를 받는다. 서버는 존재·해시·카탈로그를 먼저 확인하므로 404·409 등의 사전 오류는 일반 HTTP JSON으로 반환한다.

성공적으로 열린 스트림은 `data: {JSON}\n\n` 형식으로 진행 이벤트를 보낸다. 그래프가 정상 종료하면 마지막 `FinalAnswer` 또는 `WorkflowFailed`의 `result`에 전체 실행 결과가 들어가며 마지막 프레임은 `data: [DONE]\n\n`이다. 예상하지 못한 스트림 처리 오류에는 실패 설명과 알려진 실행·정의 ID를 보낸다. 최종 이벤트는 한 번만 보내며, 확정 결과 뒤의 정리 오류가 추가 종료 결과를 만들지 않는다.

브라우저에서는 `fetch` POST로 읽는다. 이 경로는 GET 실행이나 자동 재접속 실행을 제공하지 않는다. 이벤트 영구 저장·누락 이벤트 재생은 없으며 연결이 종료되면 해당 요청의 그래프도 정리한다. 현재 `AnswerToken`은 검증 후 코드가 합성한 보고를 한 번 전달한다. 미검증 모델 JSON은 중계하지 않는다.

<!-- [추가] 구현한 웹 프록시·클라이언트가 HTTP 거부와 스트림 중단을 구분하는 방법이다. -->
8600 포트 프록시는 명시된 워크플로 API 7개만 전달한다. 응답 헤더를 확인한 뒤 SSE를 열기 때문에 409 같은 사전 오류가 HTTP 200으로 바뀌지 않는다. 정상 스트림은 바이트 그대로 중계하며, 브라우저는 UTF-8 분할·여러 SSE 줄·프레임 경계를 조립한다. 최종 결과와 `[DONE]`을 모두 확인하고 읽기 자원을 정리한다.

프록시 통신 오류 `UPSTREAM_UNAVAILABLE`·`UPSTREAM_TIMEOUT`·`INVALID_UPSTREAM_RESPONSE`·`UPSTREAM_STREAM_ERROR`는 원격 실행 완료를 확정하지 못한 오류다. 화면은 이를 완료 확인 불가로 표시하고 자동 재실행하지 않는다. 완성된 프레임 경계의 전송 오류는 구조화된 오류로 전달한다. 부분 프레임이 남거나 관찰 버퍼 한도를 넘었다면 종료 프레임을 합성하지 않고 연결 오류로 알린다. 서버가 확정한 최종 결과를 이미 받은 경우에는 결과를 유지하고 종료 확인 문제만 알린다.

## 오류와 설정

| HTTP | 상황 |
|---|---|
| 422 | 잘못된 입력·지원하지 않는 요청·생성 검증 실패·잘못된 해시 형식 |
| 404 | 저장된 워크플로 없음 |
| 409 | 미리보기 해시 불일치·카탈로그 변경·저장 정의 손상 |
| 502 | 모델 연동·MCP 조회·보고 작성 실패 |
| 503 | 서비스 준비 전·카탈로그 읽기 실패 |
| 504 | 생성 또는 전체 실행 시간 초과 |
| 500 | 저장소 작업 실패 |

<!-- [추가] 요청 스키마·서비스·실행 오류는 서로 다른 공개 응답 형태를 가진다. -->
서비스 오류는 `error.code`, `error.message`, `error.details`로 설명한다. 필수 필드 누락·추가 필드·타입·길이 등 FastAPI 요청 검증의 422 응답은 기본 `detail` 배열 형식이다. 실행 중 실패한 JSON 응답은 전체 `WorkflowRunResult`를 유지하므로 `error.code`·`error.stage`·`error.message`와 이미 완료한 조회 자료를 함께 확인할 수 있다.

런북을 조회한 경로에서 요약의 구조·문단 참조·근거 밖 수치·한정된 실행 근거 검사 또는 합성 보고의 형식 검사에 세 번 실패하면 `REPORT_VALIDATION_FAILED`, `report_valid=false`, 빈 `final`로 반환한다. 검토용 `draft`에는 마지막 모델 JSON이나 합성 보고가 남을 수 있다. 런북 0건의 코드 사실 보고가 형식 검사에 실패하면 모델 재작성 없이 `REPORT_ERROR`로 종료한다. 모델 거절·연동 오류·시간 초과도 세 번까지 반복하지 않고 해당 오류로 종료한다. 조회 전용 실행에서 재시작 등의 완료를 주장하는 명시적 표현도 반려한다. `report_valid=true`는 모든 수치·인과·표현의 사실성을 보장하지 않는다. [검사 동작과 한계](workflow_quality_improvements.md)를 참고한다.

| 환경변수 | 기본값 | 역할 |
|---|---|---|
| `WORKFLOW_DB_PATH` | 프로젝트 루트 `workflow.db` | 별도 정의 DB 경로 |
| `WORKFLOW_GENERATION_TIMEOUT_SECONDS` | `120` | 두 번의 생성 전체 제한 |
| `WORKFLOW_EXECUTION_TIMEOUT_SECONDS` | `120` | 조회·재시도 대기·보고 전체 제한 |
| `WORKFLOW_MCP_ATTEMPTS` | `3` | 읽기 Tool의 일시적 타임아웃 총 시도 수, 1~3 |
| `WORKFLOW_MCP_RETRY_DELAY` | `0.3` | 재시도 기본 대기, 초 |
| `OLLAMA_WORKFLOW_MODEL` | 현재 Ollama quality 모델(기본 `qwen3:8b`) | 일반 진단 fast와 분리한 워크플로 생성 모델 |

서버는 기존 MCP 연결·보고 모델을 공유하고, Ollama의 워크플로 생성 모델은 별도로 선택한다. `/health`의 `workflow_llm`으로 실제 생성 모델을 확인할 수 있다. 기존 최초 호출 타임아웃 실습도 새 API에서는 일시적 오류 재시도로 처리하며, 영구 오류는 즉시 실패한다. 서비스 단독 생성자의 기본 시도 수는 1이다. [최초 OpenAI 평가](workflow_openai_results.md)에서 발견한 오류를 보완한 결과와 남은 한계는 [회귀 결과](workflow_openai_regression_results.md)에 기록했다. [Ollama 두 요청의 생성·분기 점검](workflow_ollama_local.md)은 별도 기록이며 전체 보고 품질 평가는 후속 항목이다.

<!-- [추가] 단위 테스트는 .env의 서버용 추적 설정과 분리해 외부 전송을 끈다. -->
```bash
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false ./.venv/bin/python3 -B -m unittest discover -s tests -v
node --test tests/test_workflow_stream.cjs tests/test_workflow_ui.cjs
```
