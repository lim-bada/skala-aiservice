// [추가] 새 파일 전체: Node 표준 도구로 워크플로 Vue setup의 요청·실행 상태 계약을 검증한다.
// [추가] Vue 대역은 ref/computed와 수명주기 제어만 제공한다. 템플릿·DOM·실제 Vue 렌더 검증은 아니다.
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'web', 'workflow.js'), 'utf8');

// [추가] API가 제공하는 구조와 동일한 작은 정의를 호출마다 새 객체로 만든다.
function recordFixture() {
  return {
    workflow_id: 'workflow-1', name: '결제 조건 점검', query: '점검', status: 'draft',
    definition_hash: 'a'.repeat(64),
    definition: { schema_version: 1, name: '생성 이름', steps: [
      { id: 'metrics', type: 'monitoring', system: 'payments-service' },
      { id: 'branch', type: 'if', condition: { ref: 'metrics', field: 'cpu_percent', op: '>=', value: 80 },
        then_steps: [{ id: 'cpu', type: 'runbook', incident_type: 'cpu-spike' }],
        else_steps: [{ id: 'memory', type: 'runbook', incident_type: 'memory-leak' }] },
    ] },
  };
}

// [추가] 성공과 보고 실패가 같은 조회 자료·초안을 보존하는 최종 결과를 준비한다.
function resultFixture(success = true) {
  return {
    status: success ? 'completed' : 'failed', report_valid: success,
    final: success ? '1) 확인된 자료에 근거한 최종 보고입니다.' : '',
    draft: '검토 전 초안입니다.', run_id: 'run-1234567890',
    error: success ? null : { code: 'REPORT_ERROR', message: '보고 작성 실패' },
    execution: { results: [{ node_id: 'metrics', source: 'monitoring', data: { system_name: 'payments-service', cpu_percent: 80 } }], trace: [] },
    report_attempts: [{ usage: { measurement: 'estimated' } }], elapsed_ms: 250,
    input_tokens: 5, output_tokens: 5, total_tokens: 10,
  };
}

// [추가] 브라우저 fetch 응답에서 이 컴포넌트가 실제 사용하는 JSON 부분만 대체한다.
function jsonResponse(data, status = 200, type = 'application/json') {
  return { ok: status < 400, status, headers: { get: () => type }, json: async () => data };
}

// [추가] 각 사례는 별도 VM·상태·요청 기록을 사용하며 네트워크 대신 명시적 응답만 허용한다.
function setupUI({ fetcher, consume } = {}) {
  const calls = [], mounted = [];
  let unmount;
  const Vue = {
    ref: value => ({ value }),
    computed: getter => ({ get value() { return getter(); } }),
    onMounted: callback => mounted.push(callback),
    onBeforeUnmount: callback => { unmount = callback; },
  };
  const window = { WorkflowStream: { consume: consume || (async (_response, callback) => {
    const ending = { type: 'FinalAnswer', result: resultFixture() };
    callback(ending); return ending;
  }) } };
  const fetch = async (url, options = {}) => {
    calls.push({ url, options });
    return fetcher ? fetcher(url, options) : jsonResponse(recordFixture());
  };
  vm.runInNewContext(source, { window, Vue, fetch, AbortController, URLSearchParams });
  const panel = window.WorkflowUI.WorkflowPanel;
  const ui = panel.setup({ backend: 'fake' });
  return { ui, panel, calls, mounted, unmount: () => unmount() };
}

// [추가] 생성 응답은 미리보기만 선택하고 MCP 실행 요청을 자동으로 보내지 않는다.
test('generation previews all four nodes without automatic execution', async () => {
  const { ui, calls } = setupUI();
  ui.query.value = '점검';
  await ui.generate();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/workflows/generate');
  assert.equal(ui.nodeCount.value, 4);
  assert.equal(ui.outcome.value, 'idle');
  assert.equal(ui.result.value, null);
});

// [추가] 실제 HTTP 422 Response를 JSON 처리 경로로 읽어 최종 검증 오류와 실행 전 상태를 확인한다.
test('generation HTTP 422 exposes final validation code path and message without execution', async () => {
  const finalIssues = [
    { code: 'INTENT_MISMATCH', path: 'steps[1].condition.ref', message: '조건이 요청에서 지정한 시스템의 조회 결과를 참조하지 않습니다.' },
    { code: 'INVALID_REFERENCE', path: 'steps[2].condition.ref', message: '조건보다 앞선 최상위 모니터링을 참조하세요.' },
  ];
  const payload = { error: {
    code: 'VALIDATION_FAILED', message: '두 번의 생성 후에도 유효한 워크플로를 만들지 못했습니다.',
    details: { compilation: { valid: false, workflow: null, errors: finalIssues,
      attempts: [
        { number: 1, errors: [{ code: 'INVALID_SCHEMA', path: '$', message: '이전 후보 오류입니다.' }] },
        { number: 2, errors: finalIssues },
      ] } },
  } };
  const { ui, calls } = setupUI({ fetcher: async () => new Response(JSON.stringify(payload), {
    status: 422, headers: { 'Content-Type': 'application/json' },
  }) });
  ui.query.value = '두 번째 시스템을 조건에 참조해줘.';
  await ui.generate();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/workflows/generate');
  assert.equal(ui.error.value.code, 'VALIDATION_FAILED');
  assert.equal(ui.error.value.status, 422);
  assert.deepEqual(JSON.parse(JSON.stringify(ui.validationIssues.value)), finalIssues);
  assert.equal(ui.selected.value, null);
  assert.equal(ui.result.value, null);
  assert.equal(ui.outcome.value, 'idle');
  assert.equal(ui.busy.value, '');
});

// [추가] 최종 배열이 비어 있으면 마지막 시도의 검증 오류만 표시하고 후속 성공 때 제거한다.
test('generation errors fall back to the last attempt and clear on the next success', async () => {
  const lastIssue = { code: 'INTENT_MISMATCH', path: 'steps[1].else_steps', message: '거짓 분기의 런북 조회가 다릅니다.' };
  let failed = true;
  const { ui, calls } = setupUI({ fetcher: async () => failed
    ? new Response(JSON.stringify({ error: { code: 'VALIDATION_FAILED', message: '생성 검증 실패', details: {
      compilation: { errors: [], attempts: [
        { number: 1, errors: [{ code: 'INVALID_SCHEMA', path: '$', message: '첫 시도 오류' }] },
        { number: 2, errors: [lastIssue] },
      ] },
    } } }), { status: 422, headers: { 'Content-Type': 'application/json' } })
    : jsonResponse(recordFixture()) });
  ui.query.value = '조건에 따라 조회해줘.';
  await ui.generate();
  assert.deepEqual(JSON.parse(JSON.stringify(ui.validationIssues.value)), [lastIssue]);
  failed = false;
  await ui.generate();
  assert.equal(ui.error.value, null);
  assert.deepEqual(Array.from(ui.validationIssues.value), []);
  assert.equal(ui.selected.value.workflow_id, 'workflow-1');
  assert.equal(calls.length, 2);
});

// [추가] 상세 정보 형식이 다르거나 잘못된 항목이 있어도 UI에서 가짜 오류를 만들지 않는다.
test('unstructured generation HTTP 422 details do not become invented validation rows', async () => {
  for (const details of [null, { errors: [{ code: 'X', path: '$', message: '다른 오류 계약' }] },
    { compilation: { errors: [null, { code: 'X', path: '$' }, '문자열'], attempts: '잘못된 배열' } },
    { compilation: { errors: [], attempts: [{ errors: [{ code: 'X', path: '$', message: '이전 오류' }] }, { errors: [] }] } }]) {
    const { ui } = setupUI({ fetcher: async () => new Response(JSON.stringify({ error: {
      code: 'VALIDATION_FAILED', message: '요청 확인이 필요합니다.', details,
    } }), { status: 422, headers: { 'Content-Type': 'application/json' } }) });
    ui.query.value = '점검';
    await ui.generate();
    assert.equal(ui.error.value.message, '요청 확인이 필요합니다.');
    assert.deepEqual(Array.from(ui.validationIssues.value), []);
    assert.equal(ui.selected.value, null);
  }
});

// [추가] 실행은 사용자가 선택한 정의의 해시만 POST하고 입력 정의를 다시 보내지 않는다.
test('explicit execution posts only the preview definition hash', async () => {
  const { ui, calls } = setupUI();
  await ui.load('workflow-1');
  await ui.execute();
  const request = calls.at(-1);
  assert.equal(request.url, '/workflows/workflow-1/execute/stream');
  assert.equal(request.options.method, 'POST');
  assert.deepEqual(JSON.parse(request.options.body), { definition_hash: 'a'.repeat(64) });
  assert.equal(ui.outcome.value, 'completed');
  assert.equal(ui.confirmed.value, true);
});

// [추가] HTTP 실패의 전체 실행 결과를 버리지 않고 완료 자료와 미확정 보고를 함께 남긴다.
test('HTTP 502 full failure results preserve observations and unconfirmed draft', async () => {
  const failed = resultFixture(false);
  failed.facts_report = '1) CPU 80% 2) 모니터링 1회, 런북 1회 3) 모델 요약 미포함';
  const { ui } = setupUI({ consume: async () => { throw Object.assign(new Error('보고 실패'), { status: 502, result: failed }); } });
  await ui.load('workflow-1'); await ui.execute();
  assert.equal(ui.outcome.value, 'failed');
  assert.equal(ui.confirmed.value, false);
  assert.equal(ui.observations.value.length, 1);
  assert.equal(ui.draft.value, failed.draft);
  assert.equal(ui.error.value.code, 'REPORT_ERROR');
  assert.equal(ui.result.value.facts_report, failed.facts_report);
});

// [추가] 빈 런북 분기는 0차 모델 보고 대신 실행 기록 작성과 0토큰을 표시한다.
test('facts-only reports display no model call and deterministic timeline labels', () => {
  const { ui } = setupUI();
  ui.result.value = { ...resultFixture(), facts_report: '확인된 사실', execution: { status: 'completed' }, report_attempts: [], total_tokens: 0 };
  assert.equal(ui.tokenNote.value, '보고 모델 호출 없음 · 0토큰');
  assert.match(ui.eventText({ type: 'DraftWritten', source: 'execution', attempt: 0, length: 300 }), /실행 기록에서 사실 보고 작성/);
  assert.doesNotMatch(ui.eventText({ type: 'ReportValidation', attempt: 0, valid: true }), /0차/);
});

// [추가] HTTP 연결 자체는 성공했어도 최종 이벤트 전 EOF는 실제 완료·실패로 단정하지 않는다.
test('EOF before a terminal keeps partial trace and marks completion unknown', async () => {
  const { ui } = setupUI({ consume: async (_response, callback) => {
    callback({ type: 'StepStarted', node_id: 'metrics', status: 'started' });
    callback({ type: 'AnswerToken', attempt: 1, delta: '미완료 초안' });
    throw Object.assign(new Error('EOF'), { status: 200, code: 'STREAM_EOF' });
  } });
  await ui.load('workflow-1'); await ui.execute();
  assert.equal(ui.outcome.value, 'unknown');
  assert.equal(ui.confirmed.value, false);
  assert.equal(ui.states.value.metrics.status, 'started');
  assert.equal(ui.draft.value, '미완료 초안');
  assert.match(ui.error.value.message, /자동 재실행하지 않습니다/);
});

// [추가] 확정 보고 뒤 정리·종료 신호 오류가 발생해도 최종 결과를 실패로 바꾸지 않는다.
test('EOF after a terminal preserves the confirmed result and adds a notice', async () => {
  const ending = { type: 'FinalAnswer', result: resultFixture() };
  const { ui } = setupUI({ consume: async (_response, callback) => {
    callback(ending); throw Object.assign(new Error('EOF'), { status: 200, terminal: ending });
  } });
  await ui.load('workflow-1'); await ui.execute();
  assert.equal(ui.outcome.value, 'completed');
  assert.equal(ui.result.value.final, ending.result.final);
  assert.equal(ui.confirmed.value, true);
  assert.match(ui.notice.value, /확정한 결과를 유지/);
});

// [추가] 502·504 프록시 응답은 백엔드 실행 여부를 확정할 수 없어 unknown으로 분류한다.
test('proxy gateway failures including 504 remain unknown rather than preflight rejection', async () => {
  for (const [code, status] of [['UPSTREAM_TIMEOUT', 504], ['UPSTREAM_UNAVAILABLE', 502], ['INVALID_UPSTREAM_RESPONSE', 502]]) {
    const { ui } = setupUI({ consume: async () => { throw Object.assign(new Error('프록시 원인'), { code, status, details: { source: 'proxy' } }); } });
    await ui.load('workflow-1'); await ui.execute();
    assert.equal(ui.outcome.value, 'unknown', code);
    assert.equal(ui.error.value.code, code);
    assert.equal(ui.error.value.details.reason, '프록시 원인');
    assert.equal(ui.error.value.details.details.source, 'proxy');
  }
});

// [추가] SSE로 전달된 프록시 연결 단절도 백엔드가 확정한 실패처럼 표시하지 않는다.
test('proxy stream error terminal preserves progress and reports unknown', async () => {
  const { ui } = setupUI({ consume: async (_response, callback) => {
    callback({ type: 'StepStarted', node_id: 'metrics', status: 'started' });
    const ending = { type: 'WorkflowFailed', error: { code: 'UPSTREAM_STREAM_ERROR', message: 'read error' } };
    callback(ending); return ending;
  } });
  await ui.load('workflow-1'); await ui.execute();
  assert.equal(ui.outcome.value, 'unknown');
  assert.equal(ui.states.value.metrics.status, 'started');
  assert.equal(ui.error.value.details.reason, 'read error');
  assert.equal(ui.result.value, null);
  // [추가] 상태 제목뿐 아니라 진행 문구도 실행 실패와 완료 불명을 구분한다.
  assert.match(ui.eventText(ui.timeline.value.at(-1)), /완료 여부 확인 불가/);
  assert.match(ui.eventText({ type: 'StepStarted', node_id: 'branch', node_type: 'if' }), /조건 계산 시작/);
});

// [추가] 존재·해시·검증 사전 거부는 실행 전 상태를 유지하고 409에는 다시 조회할 근거를 남긴다.
test('known 404 409 and 422 preflight rejections stay idle', async () => {
  for (const status of [404, 409, 422]) {
    const { ui } = setupUI({ consume: async () => { throw Object.assign(new Error('사전 거부'), { status, code: 'PREFLIGHT_ERROR' }); } });
    await ui.load('workflow-1'); await ui.execute();
    assert.equal(ui.outcome.value, 'idle');
    assert.equal(ui.error.value.status, status);
    assert.equal(ui.conflict.value, status === 409);
    assert.equal(ui.result.value, null);
  }
});

// [추가] 늦게 도착한 이전 목록은 새 페이지와 선택한 미리보기를 덮어쓰지 못한다.
test('late list response cannot overwrite the newer page or selection', async () => {
  const pending = [];
  const { ui } = setupUI({ fetcher: url => url.startsWith('/workflows?')
    ? new Promise(resolve => pending.push(resolve)) : jsonResponse(recordFixture()) });
  await ui.load('workflow-1');
  const first = ui.refreshList(0), second = ui.refreshList(20);
  pending[1](jsonResponse({ workflows: [{ workflow_id: 'new' }] })); await second;
  pending[0](jsonResponse({ workflows: [{ workflow_id: 'old' }] })); await first;
  assert.equal(ui.rows.value[0].workflow_id, 'new');
  assert.equal(ui.offset.value, 20);
  assert.equal(ui.selected.value.workflow_id, 'workflow-1');
});

// [추가] 한 작업 중 다른 생성·로드를 막고 실제 컴포넌트 해제 뒤에는 응답을 적용하지 않는다.
test('busy guard prevents overlapping actions and unmount aborts pending work', async () => {
  let finish, signal;
  const { ui, calls, unmount } = setupUI({ fetcher: (_url, options) => {
    signal = options.signal; return new Promise(resolve => { finish = resolve; });
  } });
  const pending = ui.load('workflow-1');
  ui.query.value = '점검'; await ui.generate(); await ui.load('other');
  assert.equal(calls.length, 1);
  unmount(); assert.equal(signal.aborted, true);
  finish(jsonResponse(recordFixture())); await pending;
  assert.equal(ui.selected.value, null);
});

// [추가] 상속 속성과 같은 ID도 실행 전·실행 중 상태로 정상 표시하고 프로토타입을 바꾸지 않는다.
test('prototype-named node IDs use own state keys and cannot pollute state maps', async () => {
  const { ui, panel } = setupUI({ consume: async (_response, callback) => {
    callback({ type: 'StepStarted', node_id: 'constructor', status: 'started' });
    callback({ type: 'StepSkipped', node_id: '__proto__', status: 'skipped' });
    throw new Error('연결 단절');
  } });
  const cardProps = { step: { id: 'constructor' }, states: {} };
  const card = panel.components.WorkflowStepCard.setup(cardProps);
  assert.equal(card.state.value.status, 'waiting');
  await ui.load('workflow-1'); await ui.execute();
  cardProps.states = ui.states.value;
  assert.equal(card.state.value.status, 'started');
  assert.equal(Object.getPrototypeOf(ui.states.value), null);
  assert.equal(Object.hasOwn(ui.states.value, '__proto__'), true);
  assert.equal(ui.states.value.__proto__.status, 'skipped');
});

// [추가] API의 HTML 오류 페이지를 JSON이나 새 실행 정의로 오인하지 않는다.
test('non-JSON API content is rejected before updating selection', async () => {
  const { ui } = setupUI({ fetcher: async () => jsonResponse({}, 200, 'text/html') });
  await ui.load('workflow-1');
  assert.equal(ui.error.value.code, 'INVALID_CONTENT_TYPE');
  assert.equal(ui.selected.value, null);
  assert.equal(ui.outcome.value, 'idle');
});
