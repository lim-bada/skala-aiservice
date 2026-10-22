<!-- [추가] 새 파일 전체: 조건부 워크플로 평가의 재현 방법·지표·실측 범위·전송 내용을 설명한다. -->
<!-- [추가] 로컬 전용 점검 문서의 링크를 제거하고 공개 문서의 변경 설명은 유지한다. -->
<!-- [추가] GitHub 미포함 발표 자료·평가 원기록은 별도 자료 또는 로컬 경로로 표시한다. -->
# 워크플로 비교 평가

작성일: 2026-10-21. 문서 대조일: 2026-10-22. 구체적 전송 승인을 받은 뒤 **당시 코드로 실제 OpenAI 소규모 연동과 dev/holdout 24문항 평가를 완료했다.** [실측 결과·실패 사례](workflow_openai_results.md)를 참고한다. 초기 환경의 연결 실패·승인 전 차단은 모델 점수에 포함하지 않았다.

<!-- [추가] 최초 실측과 동일 문항을 사용한 보완 후 회귀 결과를 구분한다. -->
당시 v3 코드의 수치는 [회귀 평가 결과](workflow_openai_regression_results.md)에 있다. 최초·중간 실패는 보존했고, v3 회귀는 의미 18/18·경로 34/34·미지원 거절 6/6이다. 현재 코드의 재측정이나 보고 사실성 전체를 검증한 점수는 아니다.

<!-- [추가] 과거 API 수치와 이후 보고 계약 변경을 구분한다. -->
이후 [보고 정확성 보완](workflow_report_accuracy.md)으로 관측·분기를 코드가 출력하고 모델은 실제 런북 문단만 요약하도록 변경했다. 새 계약의 OpenAI 평가는 아직 실행하지 않았다. 현재 workflow의 보고 전송 범위는 런북 문단·ID와 수정 피드백이며 baseline의 기존 보고 입력은 유지한다. 런북 0건이면 workflow 보고 호출은 0회다. 이전 측정의 호출·토큰 수를 현재 계약의 성능으로 해석하지 않는다.

## 문항과 실행 조건

`eval/cases/workflow_cases.json`은 지원 18개·미지원 6개, 총 24개 문항이다. dev와 holdout에 각각 지원 9개·미지원 3개를 배정했다. 경계값·상태·빈 분기·후속 단계 등을 포함한 고정 시나리오는 40개다. 이 중 지원 문항 시나리오는 34개다.

`eval/fixtures/monitoring.json`은 실제 카탈로그의 6개 시스템과 같은 필드를 사용한다. 각 시나리오는 필요한 시스템의 스냅샷을 덮어쓴다. 두 방식은 같은 관측값과 `data/runbooks/` 원문을 읽는다. 난수·현재 시각 갱신·최초 강제 실패는 넣지 않는다. 실제 MCP 네트워크 지연을 재현하는 부하 시험은 아니다.

원본 `agent.graph.build_graph()`에 MCP 호환 fixture와 같은 fast·quality 모델을 주입한다. 그래프의 라우팅·게이트·재계획·병렬 처리·조회 라운드당 0.35초 지연은 유지한다. 워크플로는 실제 컴파일러 → 정적 검증 → 임시 SQLite 저장·다시 읽기 → 실행기·보고 그래프를 사용한다. 시나리오를 바꿔 재실행할 때 절차를 재생성하지 않는다. 기존 사용자 DB는 읽거나 수정하지 않는다.

정답 AST·기대 호출은 **채점에만 사용**한다. 생성 프롬프트·수정 피드백·MCP 조회에는 넣지 않는다. fake도 현재 등록된 3개 정상 예시만 생성하며, 정답 AST를 fake 응답으로 대신 주지 않는다.

holdout은 최초 평가에서 별도 분류한 확인 문항이다. 최초(v1)의 dev/holdout 측정 사이에는 프롬프트를 수정하지 않았다. 이후 회귀 평가에서는 두 분류의 실패를 확인하고 코드·프롬프트를 보완했다. 기존 개발 예시와 표현도 겹칠 수 있으므로 외부 독립 벤치마크의 성능으로 일반화하지 않는다.

## 실행 명령

프로젝트 루트에서 실행한다. 프론트엔드·8100 서버를 켤 필요가 없다.

```bash
# 외부 호출 없이 전송할 문항·카탈로그·첫 생성 메시지를 검토한다.
./.venv/bin/python3 -B -m eval.run_workflow_eval \
  --backend openai --split all --preview-only \
  --output-dir eval/results/openai-review

# fake: 고정 응답·오류·측정 경로만 확인한다.
./.venv/bin/python3 -B -m eval.run_workflow_eval \
  --backend fake --split all --reuse-first
```

승인받은 전송 범위로 아래 순서의 실제 평가를 실행했다. 이후 재실행도 허용된 전송 범위 안에서 진행한다. 모델은 `.env`·환경변수의 `OPENAI_FAST_MODEL`·`OPENAI_QUALITY_MODEL`을 사용하고 기본값은 `gpt-4o-mini`·`gpt-4o`다. 환경변수가 `.env`보다 우선한다.

```bash
# 소규모 생성·저장·실행·보고 확인: 최대 생성 2회 + 3개 시나리오의 보고 9회.
./.venv/bin/python3 -B -m eval.run_workflow_eval \
  --backend openai --split dev --limit 1 --mode workflow \
  --output-dir eval/results/openai-smoke-run

# 비교 평가는 개발 문항과 확인 문항을 나누어 기록한다.
./.venv/bin/python3 -B -m eval.run_workflow_eval \
  --backend openai --split dev \
  --output-dir eval/results/openai-dev
./.venv/bin/python3 -B -m eval.run_workflow_eval \
  --backend openai --split holdout \
  --output-dir eval/results/openai-holdout
```

<!-- [추가] limit은 문항 수, 하나의 문항에 여러 고정 관측값이 있음을 명시한다. -->
`--limit 1`은 문항 한 개를 고른다. 첫 문항에는 CPU 79.9·80·80.1의 시나리오 3개가 있어 위 소규모 실행은 최대 생성 2회 + 보고 9회다. 한 문항의 생성은 한 번이며 각 시나리오의 보고는 별도로 생성한다. `--reuse-first`는 첫 관측값으로 한 번 더 재실행해 생성 호출이 0회인지 확인한다.

<!-- [추가] 과거 측정 명령과 재실행 방법, 보고 생략 옵션의 모델 호출 범위를 구분한다. -->
위 OpenAI 명령은 당시 측정에 사용한 명령이다. 이미 결과가 있는 디렉토리는 덮어쓸 수 없으므로 재실행 시 새 `--output-dir`를 지정한다. 현재 명령은 현재 소스를 실행하므로 과거 소스 해시의 결과를 그대로 재현한다는 뜻은 아니다.

`--report-mode none`은 조회 경로 전용이다. baseline의 quality 모델 보고 의존성을 고정 텍스트로 대체하고, 워크플로는 보고를 호출하지 않는다. baseline의 route·plan·analyze·chat과 워크플로 생성 호출까지 생략하는 옵션은 아니다. 이 설정과 대체 여부를 manifest에 남기며 full 결과와 섞지 않는다. 기본값은 full이다.

## 지표와 결과 파일

| 지표 | 기준 |
|---|---|
| 최초·최종 의미 일치 | 정상 종료·정적 검증을 통과한 AST의 순서·대상·필드·연산·기준·참조·양쪽 분기 비교 |
| 의미 비교의 허용 차이 | 표시 이름·노드 ID·80과 80.0은 무시. 반대 조건과 분기 전체를 함께 뒤집은 동치도 허용 |
| 미지원 거절 | `UNSUPPORTED_REQUEST`만 정답 거절. 모델 거절·통신 오류·검증 실패를 섞지 않음 |
| 실행 경로 일치 | 기대한 도메인 호출의 시작 순서와 대상이 전부 일치 |
| 호출 집합 일치 | 병렬 baseline의 순서 영향을 분리해 호출 종류·대상·중복 횟수 비교 |
| 추가·중복 호출 | 기대 호출보다 많은 실제 호출의 수. 요청 순서 오류와 별도로 기록 |
| 대상 포함 | 기대 모니터링 대상 중 실제 조회한 고유 대상의 비율 |
| 잘못된 절차 차단 | 무효 AST 12개의 실제 실행기 거절과 MCP 도메인 조회 0회 확인 |
| 시간 | 생성·저장 시간, 시나리오별 실행·보고, 첫 생성+실행, 저장 재실행을 분리 |
| 사용량 | 모델 호출별·단계별 입력·출력·전체 토큰. 누락은 null, fake는 추정으로 표시 |
| MCP 호출 | 모니터링 Tool·런북 Resource·탐색·프롬프트를 분리 |

baseline에는 워크플로 AST·명시적 지원 거절 계약이 없으므로 해당 지표를 임의로 0점 처리하지 않는다. 생성 실패도 지원 시나리오의 경로 점수 분모에 포함하며, 실행하지 않은 경우 지연은 null로 남긴다. 완료 실행 지연 중앙값에는 완료 수를 함께 제시한다.

<!-- [추가] 수정 생성·보고 재작성과 통신 재시도, 사용량 미수신과 무호출을 구별한다. -->
생성은 검증에서 반려되면 최대 2회, 보고 요약은 검증에서 반려되면 최대 3회까지 시도한다. 통신 오류를 같은 횟수만큼 자동 재시도한다는 뜻은 아니다. 모델 호출 수는 계측된 메서드 호출 시도 수이며 청구 요청 수와 동일하다고 단정하지 않는다. 사용량이 한 호출이라도 누락되면 해당 합계 필드는 null이다. 실제 호출이 0회일 때만 `measurement=no_calls`·토큰 0으로 표시한다. 응답을 받지 못한 실패·취소도 공급자에서 처리·청구됐을 가능성이 있어 null을 무료 호출로 해석하지 않는다.

각 실행 폴더에 다음을 쓴다.

- `payload_preview.json`: 목적지·문항·카탈로그·첫 생성 메시지와 보고 전송 자료의 범주.
- `results.json`: 코드·문항·fixture 해시, 모델·버전·조건, 후보·검증 오류, 조건 trace·조회·보고, 호출별 지연·사용량·실패.
- `report.md`: 분모가 있는 요약 표와 실패 문항, 해석 범위.

<!-- [추가] 초기 결과 형식에 없던 manifest 필드와 이후 시점의 상태 판정을 구분한다. -->
초기 산출물에는 이후 추가한 `evaluation_role`·`workflow_report_contract` 등이 없을 수 있다. 필드 부재를 현재 계약의 실측으로 간주하지 않는다. 합산 JSON의 `current_source_matches_v3`도 합산 파일을 만든 당시의 비교 결과이며 현재 소스가 계속 같다는 보증이 아니다. 과거 `source_sha256`과 원자료는 갱신하지 않는다.

인증·모델 접근·요금 한도 오류(401·403·404·429)와 연결 오류는 후속 실제 호출을 중단한다. 상태는 `blocked`, CLI 종료 코드는 2다. 전 문항이 모델 오류 등으로 유효한 결과 없이 끝나면 `failed`, 종료 코드 1이다. 실제 계측을 끝낸 `completed`가 모든 문항 정답을 뜻하지는 않는다.

생성 120초·실행 120초가 기본이며 `--timeout`으로 바꾼다. 최초 측정 당시 baseline의 동기 HTTP가 이벤트 루프를 막을 수 있다는 한계를 manifest에 기록했다. 이후 응답 정체 수정으로 해당 모델 호출을 스레드에 분리했다. 현재 그래프 대기는 취소할 수 있지만 이미 실행 중인 동기 HTTP·모델 계산은 요청 제한 시간까지 계속될 수 있다. 과거 평가 수치를 수정 후 성능으로 해석하지 않는다. 가상 USD 필드를 실제 청구액으로 해석하지 않는다. LangSmith 자동 전송은 평가 CLI에서 끈다.

## 실제 OpenAI 전송 범위

목적지는 `https://api.openai.com/v1/chat/completions`다. 승인 시 프로젝트의 API 키를 인증 헤더에 사용한다. 키 값은 미리보기·로그·결과 파일에 기록하지 않는다.

<!-- [추가] 현재 전송 경로와 과거 자유 서술 보고의 입력을 정확히 구분한다. -->
현재 생성 호출에는 선택한 평가 문항, 6개 모의 시스템·6개 런북의 카탈로그 식별자, JSON 스키마·생성 프롬프트와 필요한 수정 피드백을 보낸다. baseline에는 기존 질의·수집 자료·보고 피드백을 보내고, workflow 보고에는 실제 조회한 런북의 제목·문단·자료 ID와 요약 검증 피드백만 보낸다. 관측값·실행 trace는 workflow 보고 모델의 현재 입력에서 제외한다. 과거 v1~v3의 자유 서술 보고에는 질의·수집 자료·trace도 사용했다. `.env` 원문·기존 대화·사용자 저장 DB는 전송하지 않는다. 생성 입력 예시는 전송 미리보기 (`eval/results/openai-review/payload_preview.json`, 로컬 산출물)에 있으며, 과거 미리보기는 당시 계약의 자료이므로 현재 입력은 `--preview-only`로 새로 만든 파일과 `payload_preview()`·`build_workflow_report_messages()`에서 확인한다.

전체 24문항·40시나리오 full 비교는 `--reuse-first`가 없을 때 구조상 최대 생성 48회, baseline 240회, 워크플로 보고 120회다. 실제 호출은 사전 거절·실패·재작성 여부와 런북 조회 유무에 따라 더 적다. `--reuse-first`를 추가하면 생성에 성공한 문항마다 첫 시나리오 실행·보고가 한 번 더 늘어난다. 소규모 연동을 먼저 확인하고 오류가 있으면 비교 평가를 시작하지 않는다.

## 검증과 남은 작업

최초 평가 도구 추가 당시(2026-10-21) 전체 Python **308개**, Node **42개**가 통과했다. v3 품질 보완 당시에는 Python **357개**, Node **42개**가 통과했다. 이는 당시 검증 기록이며 현재 테스트 수를 뜻하지 않는다. 정답 AST를 실제 실행기에 직접 넣은 지원 시나리오는 **34/34**, 무효 AST의 조회 전 차단은 **12/12**다. 이는 실행 제어의 결정적 검증이다.

로컬 fake 전체 실행 보고 (`eval/results/fake-final/report.md`, 로컬 산출물)에는 24문항·40시나리오의 측정을 남겼다. fake 생성기는 등록된 정상 예시 3개만 생성해 지원 문항의 의미 일치는 3/18이었다. 원본 fake 라우터는 이 문항들을 전부 chat으로 분류했다. 이 결과는 계측 도구의 동작 확인이며 실제 자연어 성능·기존 대비 우열의 근거가 아니다. 실제 OpenAI 결과와 섞지 않는다.

<!-- [추가] 평가 코드는 제품 실행 경로와 분리하고 기존 어댑터에는 계측 필드만 보강했다. -->
| 추가·수정 파일 | 역할 |
|---|---|
| `eval/run_workflow_eval.py` | 문항 선택·원본/개선 실행·단계별 사용량·결과 JSON/Markdown·전송 미리보기 |
| `eval/runtime.py` | 고정 MCP 대역·호출 기록·모델 계측·실제 컴파일러/저장소/그래프 실행·무효 AST 차단 측정 |
| `eval/scoring.py` | ID·이름을 제외한 의미 비교, 논리적 동치, 실행 순서·중복·대상 채점 |
| `eval/cases/workflow_cases.json` | dev/holdout 문항·정답·고정 시나리오·기대 호출 |
| `eval/fixtures/monitoring.json` | 6개 시스템의 고정 모의 관측값 |
| `agent/openai_llm.py` | 기존 `_chat`의 요청·응답 동작을 유지하고 실측 입출력 토큰·누락 상태 추가 |
| `tests/test_workflow_eval_*.py`, `tests/test_openai_legacy_usage.py` | 평가기·정답·회귀·사용량 테스트 24개 |
| `.gitignore`, `sh/make_student_zip.sh` | 로컬 평가 산출물의 소스·배포본 제외 |

```bash
./.venv/bin/python3 -B -m unittest discover -s tests -v
node --test tests/test_workflow_stream.cjs tests/test_workflow_ui.cjs
```

위 파일 표의 테스트 24개는 최초 평가 도구 추가 당시의 범위다. 이후 보완한 현재 테스트 수와 구분한다.

추가 테스트는 모든 지원 정답 AST의 34개 시나리오 경로, 무효 AST 12개의 호출 전 차단, 원본 MCP 프롬프트 동일성, 기존 baseline 진단 경로, 저장 재실행·분기 변경, 이름·ID·동치 비교식, 순서·참조 오류, 사용량 누락·민감 오류 숨김·초기 API 실패 중단을 확인한다.

실제 OpenAI 생성·보고 연동과 최초 dev·holdout 비교 측정·실패 검토를 완료했다. 이후 [생성·보고 품질 보완](workflow_quality_improvements.md)을 추가했다. fake 결과에서 실제 자연어 이해·보고 근거 정확도·서비스 속도 향상을 주장하지 않는다. 보고 형식·한정된 근거 검증과 이 평가의 절차 의미 검사는 보고 내용 전체의 진실성을 보증하지 않는다.

<!-- [추가] 실패 확인 뒤 같은 문항을 다시 사용한 결과의 평가 역할을 명시한다. -->
## 보완 뒤 회귀 평가

기존 24문항은 실패를 확인한 뒤 수정하는 데 참고했다. 원래 `dev`/`holdout` 분류를 보존하되, 재측정에는 `--evaluation-role regression`을 지정한다. 이 결과를 독립된 미관측 holdout 성능으로 해석하지 않는다. 출력 폴더를 새로 지정해 최초 결과를 보존한다.

```bash
./.venv/bin/python3 -B -m eval.run_workflow_eval \
  --backend openai --split all --mode workflow --report-mode full \
  --evaluation-role regression --output-dir eval/results/openai-regression-next
```

이 명령은 보완한 workflow만 재측정한다. 최초 baseline 기록은 그대로 두며, 보완 전후 workflow를 같은 문항·스냅샷·모델 설정으로 비교한다. 새 일반화 성능을 확인할 추가 문항 평가는 별도 작업이다. 승인된 실제 API 호출에는 사용료가 발생한다.

요청 형식과 응답 완료·사용량 처리는 [OpenAI 공식 Chat Completions 문서](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)와 [공식 JSON 모드 문서](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=chat)를 확인했다. JSON 형식 성공은 서버 검증·의미 채점을 대신하지 않는다.
