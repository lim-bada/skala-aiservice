<!-- [추가] 새 파일 전체: 일반 진단이 다른 API까지 정체시킨 로컬 오류와 수정·검증을 기록한다. -->
# 일반 진단 중 상태·워크플로 조회 타임아웃

2026-10-21 신고 문장: `web-01을 조회하고 상태가 healthy면 추가 조회하지 말고, 아니면 CPU 런북을 확인해줘.`

첨부한 프론트엔드 로그의 예외는 `/health` 중계에서 발생한 `httpx.ReadTimeout`이다. 이후 카탈로그와 저장 목록도 504가 발생했다. 이 로그 자체는 모델의 JSON 검증 실패나 조건 판단 오류를 뜻하지 않는다.

기존 `agent/graph.py`의 라우팅·계획·분석은 `async def` 안에서 동기 모델 메서드를 직접 호출했다. 이 메서드가 동기 HTTP 응답을 기다리는 동안 FastAPI 이벤트 루프가 다른 요청을 처리할 수 없었다. 이 때문에 LLM을 사용하지 않는 health·카탈로그·목록도 함께 지연될 수 있었다. 웹 서버는 health 타임아웃을 처리하지 않아 추가 ASGI 500 traceback을 출력했다.

## 코드 변경

| 파일 | 변경 |
|---|---|
| `agent/graph.py` | `route`, `plan`, `analyze`를 `asyncio.to_thread()`로 호출해 동기 네트워크 대기를 이벤트 루프 밖으로 분리 |
| `agent/ollama_llm.py` | 짧은 동기 분류·계획·분석 요청에 `think=false`, `num_predict=1024` 적용 |
| `web_demo_server.py` | health·일반 JSON 진단의 타임아웃을 504, 연결 실패를 502 JSON으로 전달하고 잘못된 upstream JSON도 구분 |
| `web_chat.html` | health 오류 JSON을 정상 모델 정보로 표시하지 않고 연결 확인 실패로 표시 |
| `eval/run_workflow_eval.py` | 현재 스레드 분리·취소 동작에 맞게 baseline 제한 설명 갱신 |

기존 일반 진단의 계획·게이트·재조사·런북 선택 로직은 유지했다. `asyncio.to_thread()`로 기다리는 작업을 취소해도 이미 실행 중인 동기 HTTP와 원격 모델 계산까지 즉시 중단되는 것은 아니다. 모델 자체의 대기와 다른 API의 응답 정체를 구분한다.

## 검증

- 라우팅·계획·분석 모델을 각각 대기시킨 상태에서 실제 서버의 `/health`, `/workflows/catalog`, `/workflows`가 모델 완료 전에 모두 200을 반환했다.
- health·일반 JSON 진단의 ReadTimeout·ConnectError·잘못된 응답을 실제 ASGI 프록시 경로로 확인했다.
- 실제 로컬 `qwen3:0.6b` 라우팅을 기다리는 중에도 세 API가 모두 200을 반환했다. 해당 점검은 모델 HTTP를 30초로 제한했으며 모델 응답 자체는 ReadTimeout이었다.
- 별도 포트의 임시 Ollama와 임시 DB에서 비추론 옵션을 적용한 동일 문장의 실제 라우팅도 완료했고, 모델 대기 중 세 API는 모두 200이었다.
- 당시(2026-10-21) 전체 Python **370개**, Node **45개** 통과. 일반 채팅 inline JavaScript의 문법 검사도 통과했다. 이 숫자는 해당 수정 시점의 기록이며 최신 전체 검증과 구분한다.

실제 모델 점검은 라우팅 단계와 동시 API 응답 확인이다. 전체 진단 보고의 정확도나 전후 성능 벤치마크가 아니다. 사용자 DB와 실행 중인 서버는 변경하지 않았다.

[실행 중인 로컬 모델 점검](../eval/results/general-diagnosis-responsiveness/results.json), [별도 로컬 모델 점검](../eval/results/general-diagnosis-responsiveness/isolated_after.json)은 배포에서 제외되는 로컬 산출물이다.

## 적용

<!-- [추가] 현재 환경 예시와 일치하도록 추적을 켠 .env 기반 직접 실행을 안내한다. -->
Ollama는 실행해 둔 상태에서 실제 `.env`의 기존 값을 다음처럼 바꾼다. LangSmith 키는 [.env.example](../.env.example)을 참고해 설정한다. 서버는 `.env.example` 대신 `.env`를 읽는다.

```dotenv
LLM_BACKEND=ollama
OLLAMA_WORKFLOW_MODEL=qwen3:8b
LANGCHAIN_TRACING_V2=true
LANGCHAIN_PROJECT=capstone-all-total-ollama
```

백엔드와 웹 서버를 각각 `Ctrl+C`로 종료하고, 프로젝트 루트에서 다시 실행한다.

백엔드 터미널:

```bash
./.venv/bin/python3 server.py
```

웹 서버 터미널:

```bash
./.venv/bin/python3 web_demo_server.py
```

브라우저에서 `Cmd+Shift+R` 후 같은 요청을 다시 실행한다. 일반 진단이 진행되는 중에도 저장 목록·사용 가능한 자원과 `/health`가 응답하는지 확인한다. 모델을 기다리는 시간이 남더라도 서버 전체가 그 동안 정지해서는 안 된다.

이번 수정은 이전 [OpenAI 비교 평가](workflow_openai_regression_results.md) 이후 변경이다. 과거 측정 기록은 보존하며, 현재 코드의 성능 수치로 재사용하지 않는다.
