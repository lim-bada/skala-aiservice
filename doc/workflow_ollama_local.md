<!-- [추가] 새 파일 전체: 로컬 Ollama 조건부 생성 실패의 재현·모델 분리·재실행 방법을 기록한다. -->
# Ollama 워크플로 생성 오류 점검

2026-10-21 로컬 실행에서 신고한 두 요청의 `VALIDATION_FAILED`를 실제로 재현했다. 당시 `/health`는 일반 진단용 fast 모델 `qwen3:0.6b`, 보고용 quality 모델 `qwen3:8b`였으며 워크플로 생성에도 fast 모델을 사용했다.

## 재현과 원인

| 요청 | qwen3:0.6b의 실제 생성 오류 | 검증 결과 |
|---|---|---|
| CPU가 80% 이상이면 CPU 런북, 아니면 메모리 런북 | `>=`를 `>`로 바꾸고 `else_steps`를 비움. 수정 생성에서도 반복 | `INTENT_MISMATCH` |
| 상태가 healthy면 종료, 아니면 CPU 런북 | 요청하지 않은 시스템 4개 추가, 상태 대신 CPU 조건 생성, 전체 7노드 | `INVALID_SCHEMA` |

두 입력과 올바른 AST는 입력·구조·의도 검사를 통과한다. 첫 요청의 마침표 생략도 원인이 아니었다. Ollama HTTP 통신과 JSON 생성은 완료됐지만 후보가 지원 계약을 위반해 두 번 모두 반려됐다. 검증 실패를 강제로 통과시키거나 잘못된 후보를 실행하도록 바꾸지 않았다.

같은 프롬프트·스키마·검증기로 설치된 `qwen3:8b`를 사용했을 때 두 요청 모두 최초 1회에 통과했다. CPU 요청은 `>= 80`과 두 런북을 보존했고, healthy 요청은 동등한 반대 조건과 빈 분기를 생성했다. 실제 생성 정의로 고정 관측값의 CPU 79.9·80 및 healthy·degraded 네 실행 경로도 **4/4** 일치했다.

이는 신고한 두 문항의 로컬 연동 점검이다. Ollama 전체 정확도나 보고 품질을 측정한 벤치마크가 아니다. 이 점검에서 외부 API·사용자 DB·보고 모델 호출을 사용하지 않았다.

<!-- [추가] 수정한 서버 조립과 API·임시 저장까지 포함한 실제 로컬 연동 결과다. -->
수정한 서버의 `lifespan`·모델 선택·`/workflows/generate`·초안 저장까지 연결한 별도 ASGI 점검에서도 두 요청 모두 **HTTP 201**, 생성 각 1회, `ollama:qwen3:8b` 사용을 확인했다. 이 점검은 포트 11435의 임시 Ollama와 임시 DB·별도 MCP 자원으로 수행하고 종료했다. 기존 사용자 서버나 저장 DB를 재시작·수정하지 않았다.

## 변경 코드

- `server.py`: 일반 진단 fast 모델과 워크플로 생성 모델을 분리한다. Ollama는 `OLLAMA_WORKFLOW_MODEL`을 우선하고, 미설정 시 실제 quality 모델을 사용한다. 기본 quality 모델은 `qwen3:8b`다. fake·OpenAI의 생성 모델 선택은 기존대로 유지한다.
- `/health`: `workflow_llm`에 실제 워크플로 생성 모델 이름을 추가한다.
- `web/workflow.js`: 생성 실패의 최종 검증 오류 code·path·message를 화면 목록에 표시한다. 전체 상세 정보는 접힌 영역에서 확인한다.
- `.env.example`: `OLLAMA_WORKFLOW_MODEL` 설정 예시를 추가한다.
- 관련 테스트: 모델 선택 우선순위·기존 백엔드 호환·서비스 주입과 HTTP 422 오류 표시를 검증한다.

당시(2026-10-21) 자동화 검증은 전체 Python **364개**, Node **45개**가 통과했다. 이 숫자는 생성 모델 분리 시점의 기록이며 최신 전체 검증과 구분한다. 해당 변경의 신규 검증은 서버 수명·모델 선택 7개와 오류 표시 3개다. 오류 표시 테스트는 실제 HTTP 422 응답을 처리하는 상태·계산 값 검증이며, 실제 브라우저 DOM 렌더 검증과 구분한다.

## 적용 방법

<!-- [추가] 현재 환경 예시와 일치하도록 추적을 켠 .env 기반 모델 전환을 안내한다. -->
Ollama가 실행 중이고 `qwen3:8b`가 설치된 상태에서 실제 `.env`의 기존 설정을 다음처럼 바꾼다. LangSmith 키는 [.env.example](../.env.example)을 참고해 설정한다.

```dotenv
LLM_BACKEND=ollama
OLLAMA_WORKFLOW_MODEL=qwen3:8b
LANGCHAIN_TRACING_V2=true
LANGCHAIN_PROJECT=capstone-all-total-ollama
```

`LANGSMITH_*` 별칭이나 셸 환경변수로 같은 설정을 중복 지정했다면 함께 정리한다. 기존 백엔드 터미널을 `Ctrl+C`로 종료한 뒤 프로젝트 루트에서 다시 실행한다.

```bash
./.venv/bin/python3 server.py
```

별도 터미널에서 실제 모델을 확인한다.

```bash
curl -s http://127.0.0.1:8100/health | ./.venv/bin/python3 -m json.tool
```

`workflow_llm`이 `ollama:qwen3:8b`여야 한다. 일반 진단의 `fast_llm`은 기본값 `ollama:qwen3:0.6b`이며 `OLLAMA_FAST_MODEL`을 따로 설정했다면 그 값이 적용된다. health는 모델 선택 확인이며 실제 생성 성공을 확인하는 요청은 아니다. 웹 서버는 기존대로 두고 브라우저에서 강력 새로고침한 뒤 두 요청을 다시 생성한다.

모델 크기나 속도 때문에 다른 모델을 선택하면 동일한 생성·분기 검증을 다시 확인해야 한다. 기존 OpenAI 24문항 결과는 [별도 회귀 평가](workflow_openai_regression_results.md)이며 Ollama 점수와 섞지 않는다.

## 근거

- [로컬 재현·8b 생성·네 실행 경로 원자료](../eval/results/ollama-local-check/results.json): 소스·배포 zip에서 제외되는 로컬 산출물이다.
- [수정 서버의 실제 생성 API 점검](../eval/results/ollama-local-check/api_results.json): 두 HTTP 201과 health·실제 생성 모델을 기록했다.
- 스키마를 `format`으로 전달하는 방법은 [Ollama 공식 구조화 출력 문서](https://docs.ollama.com/capabilities/structured-outputs)를 확인했다. 이번 보완은 기존 스키마·서버 검증을 유지하고 생성 모델 선택을 분리한다.
