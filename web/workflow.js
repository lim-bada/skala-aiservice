// [추가] 새 파일 전체: 생성·미리보기·저장과 명시적인 한 번의 실행을 제공하는 Vue 패널.
(function () {
  'use strict';
  const { ref, computed, onMounted, onBeforeUnmount } = Vue;
  const examples = [
    'payments-service를 조회하고 CPU가 80% 이상이면 CPU 런북을, 아니면 메모리 런북을 확인해줘.',
    'auth-service를 먼저 조회하고 web-01을 조회해줘.',
    'payments-service를 조회하고 상태가 healthy면 종료하고, 아니면 CPU 런북을 확인해줘.',
  ];
  const labels = { started: '실행 중', completed: '완료', skipped: '건너뜀', failed: '실패', waiting: '실행 전' };
  // [추가] 게이트웨이 응답만으로는 원격 실행의 성공·실패를 확정할 수 없는 오류를 구분한다.
  const transportCodes = new Set(['UPSTREAM_TIMEOUT', 'UPSTREAM_UNAVAILABLE', 'INVALID_UPSTREAM_RESPONSE', 'UPSTREAM_STREAM_ERROR']);
  const valueText = value => value === null || value === undefined ? '미제공' : typeof value === 'object' ? JSON.stringify(value) : String(value);
  const conditionText = condition => `${condition.ref}.${condition.field}: ${valueText(condition.actual)} ${condition.op} ${valueText(condition.expected)} → ${condition.result ? '참' : '거짓'} · ${condition.selected_branch === 'then_steps' ? '참 분기' : '거짓 분기'} 선택`;
  const runbookTitle = body => String(body || '').trim().split('\n')[0].replace(/^#+\s*/, '') || '제목 없는 문서';

  // [추가] 분기 양쪽을 모두 미리 보여주고 실행 중 실제 선택·건너뜀을 노드 ID로 표시한다.
  const WorkflowStepCard = {
    name: 'WorkflowStepCard',
    props: { step: Object, states: Object },
    setup(props) {
      // [추가] constructor 같은 유효 ID를 상속 속성으로 오인하지 않고 노드 자신의 상태만 읽는다.
      const state = computed(() => Object.hasOwn(props.states, props.step.id) ? props.states[props.step.id] : { status: 'waiting' });
      return { state, labels, valueText, conditionText };
    },
    template: `
      <article class="workflow-step" :class="'workflow-state-' + state.status">
        <div class="workflow-step-heading">
          <strong>{{ step.type === 'monitoring' ? '모니터링 조회' : step.type === 'runbook' ? '런북 조회' : '조건 분기' }}</strong>
          <span class="workflow-badge">{{ labels[state.status] || state.status }}</span>
        </div>
        <code class="workflow-id">{{ step.id }}</code>
        <p v-if="step.type === 'monitoring'">
          시스템:
          <strong>{{ step.system }}</strong>
        </p>
        <p v-else-if="step.type === 'runbook'">
          장애 유형:
          <strong>{{ step.incident_type }}</strong>
        </p>
        <template v-else>
          <p class="workflow-condition">
            <code>{{ step.condition.ref }}.{{ step.condition.field }} {{ step.condition.op }} {{ valueText(step.condition.value) }}</code>
          </p>
          <p v-if="state.condition" class="workflow-condition-result">실제 판단: {{ conditionText(state.condition) }}</p>
          <!-- [추가] 조건 카드에서 참·거짓 분기의 계획과 실제 선택을 함께 표시한다. -->
          <div class="workflow-branches">
            <section class="workflow-branch" :class="{ 'workflow-branch-selected': state.condition?.selected_branch === 'then_steps' }">
              <h5>참이면 · then</h5>
              <workflow-step-card v-for="child in step.then_steps" :key="child.id" :step="child" :states="states" />
              <p v-if="!step.then_steps.length" class="workflow-muted">이 분기 추가 조회 없음</p>
            </section>
            <section class="workflow-branch" :class="{ 'workflow-branch-selected': state.condition?.selected_branch === 'else_steps' }">
              <h5>거짓이면 · else</h5>
              <workflow-step-card v-for="child in step.else_steps" :key="child.id" :step="child" :states="states" />
              <p v-if="!step.else_steps.length" class="workflow-muted">이 분기 추가 조회 없음</p>
            </section>
          </div>
        </template>
      </article>`,
  };

  // [추가] 상태 코드와 JSON 콘텐츠 유형을 검사하고 HTML 오류 본문을 사용자 데이터로 해석하지 않는다.
  async function jsonResponse(response) {
    const type = (response.headers.get('content-type') || '').split(';')[0].trim().toLowerCase();
    if (type !== 'application/json' && !/^application\/[a-z0-9!#$&^_.+-]+\+json$/.test(type)) {
      const error = new Error(`JSON 응답을 받지 못했습니다 (HTTP ${response.status}).`);
      Object.assign(error, { code: 'INVALID_CONTENT_TYPE', status: response.status });
      throw error;
    }
    let payload;
    try { payload = await response.json(); }
    catch (_) {
      const error = new Error('서버의 JSON 응답을 읽지 못했습니다.');
      Object.assign(error, { code: 'INVALID_JSON_RESPONSE', status: response.status });
      throw error;
    }
    if (!response.ok) {
      const info = payload?.error || {};
      const error = new Error(info.message || `요청에 실패했습니다 (HTTP ${response.status}).`);
      Object.assign(error, { code: info.code || 'HTTP_ERROR', status: response.status, details: info.details ?? payload?.detail ?? null, result: payload?.execution ? payload : null });
      throw error;
    }
    return payload;
  }

  // [추가] 이벤트는 실행 사실로만 표현하며 초안 작성·형식 검증과 최종 확정을 구분한다.
  function eventText(event) {
    const node = event.node_id || '';
    switch (event.type) {
      case 'WorkflowStarted': return '저장된 계획으로 실행 시작' + (typeof event.run_id === 'string' ? ` · 실행 ID ${event.run_id.slice(0, 12)}…` : '');
      // [추가] 조건 계산은 MCP 조회와 구분해 실제 작업만 표시한다.
      case 'StepStarted': return `${node} · ${event.node_type === 'if' ? '조건 계산 시작' : '조회 시작'}`;
      case 'StepCompleted': return `${node} · 조회 완료`;
      case 'StepSkipped': return `${node} · 미선택 분기, 조회하지 않음`;
      case 'StepFailed': return `${node} · 조회 실패: ${event.error?.message || '오류 설명 미제공'}`;
      case 'ConditionEvaluated': return event.condition ? `${node} · ${conditionText(event.condition)}` : `${node} · 조건 판단`;
      case 'ReportStarted': return `보고 초안 작성 시작 · ${event.attempt}차`;
      // [추가] 모델 호출 없이 작성한 사실 보고를 0차 LLM 시도로 표시하지 않는다.
      case 'DraftWritten': return event.source === 'execution'
        ? `실행 기록에서 사실 보고 작성 · ${event.length}자 (확정 대기)`
        : `런북 요약 응답 수신 · ${event.attempt}차 (검증 대기)`;
      case 'ReportValidation': return `${event.attempt ? event.attempt + '차 ' : ''}보고 검증 ${event.valid ? '통과 · 최종 확정 대기' : '반려: ' + (event.errors || []).join(' ')}`;
      case 'FinalAnswer': return '실행 완료 · 최종 보고 확정';
      // [추가] 프록시 중단 이벤트의 타임라인도 실행 실패가 확정된 것처럼 쓰지 않는다.
      case 'WorkflowFailed': return !event.result && transportCodes.has(event.error?.code)
        ? '연결 중단 · 실행 완료 여부 확인 불가'
        : `실행 실패 · ${event.error?.message || event.result?.error?.message || '오류 설명 미제공'}`;
      default: return event.type;
    }
  }

  const WorkflowPanel = {
    name: 'WorkflowPanel', components: { WorkflowStepCard },
    props: { backend: { type: String, default: '' } },
    setup(props) {
      const query = ref(''), catalog = ref(null), catalogError = ref(''), listError = ref('');
      const rows = ref([]), offset = ref(0), includeDrafts = ref(false), listBusy = ref(false);
      const selected = ref(null), saveName = ref(''), busy = ref(''), error = ref(null), notice = ref('');
      const timeline = ref([]), states = ref(Object.create(null)), draft = ref(''), draftAttempt = ref(null);
      const result = ref(null), terminal = ref(null), outcome = ref('idle');
      const requests = new Map();
      let disposed = false, serial = 0;
      const fake = computed(() => props.backend === 'fake');
      const nodeCount = computed(() => (selected.value?.definition?.steps || []).reduce((sum, step) => sum + 1 + (step.then_steps?.length || 0) + (step.else_steps?.length || 0), 0));
      const canExecute = computed(() => !busy.value && !!selected.value?.workflow_id && !!selected.value?.definition?.steps?.length && /^[a-f0-9]{64}$/.test(selected.value?.definition_hash || ''));
      const observations = computed(() => (result.value?.execution?.results || []).filter(item => item.source === 'monitoring'));
      const runbooks = computed(() => (result.value?.execution?.results || []).filter(item => item.source === 'runbook'));
      const confirmed = computed(() => result.value?.status === 'completed' && result.value?.report_valid === true && !!result.value?.final?.trim());
      const tokenNote = computed(() => {
        const measurements = (result.value?.report_attempts || []).map(item => item.usage?.measurement || 'unavailable');
        // [추가] 런북 생략 경로의 보고 모델 0회·0토큰을 미제공 사용량으로 오인하지 않는다.
        if (result.value?.execution?.status === 'completed' && !measurements.length && result.value?.facts_report) return '보고 모델 호출 없음 · 0토큰';
        if (measurements.includes('estimated')) return '문자 수 기반 추정치 포함';
        return measurements.length && measurements.every(item => item === 'reported') ? '공급자가 보고한 사용량' : '미제공 사용량 포함';
      });
      const errorDetails = computed(() => error.value?.details ? JSON.stringify(error.value.details, null, 2) : '');
      // [추가] 생성 실패의 마지막 검증 사유를 원본 JSON을 펼치지 않아도 읽을 수 있게 추출한다.
      const validationIssues = computed(() => {
        const compilation = error.value?.details?.compilation;
        if (!compilation || typeof compilation !== 'object' || Array.isArray(compilation)) return [];
        const readable = issues => Array.isArray(issues)
          ? issues.filter(issue => issue && ['code', 'path', 'message'].every(key => typeof issue[key] === 'string' && issue[key].trim()))
            .map(({ code, path, message }) => ({ code, path, message }))
          : [];
        const finalIssues = readable(compilation.errors);
        if (finalIssues.length) return finalIssues;
        // [추가] 최종 오류 배열이 없으면 마지막 시도의 오류만 사용하며 이전 실패 사유를 섞지 않는다.
        const attempts = Array.isArray(compilation.attempts) ? compilation.attempts : [];
        return readable(attempts.at(-1)?.errors);
      });
      const conflict = computed(() => error.value?.status === 409);
      const duration = computed(() => Number.isFinite(result.value?.elapsed_ms) ? `${(result.value.elapsed_ms / 1000).toFixed(2)}초` : '미제공');
      const outcomeText = computed(() => ({ idle: '실행 전', running: '실행 중', completed: '완료 · 보고 확정', failed: '실패 · 보고 미확정', unknown: '중단 · 완료 확인 불가' })[outcome.value]);

      // [추가] 별도 요청 슬롯과 토큰으로 늦은 목록·카탈로그 응답이 새 상태를 덮지 못하게 한다.
      function begin(slot) {
        requests.get(slot)?.controller.abort();
        const request = { controller: new AbortController(), token: ++serial };
        requests.set(slot, request);
        return request;
      }
      function current(slot, request) { return !disposed && requests.get(slot) === request; }
      async function fetchJSON(path, options, request) {
        return jsonResponse(await fetch(path, { ...options, signal: request.controller.signal }));
      }
      function displayError(caught) {
        error.value = { code: caught.code || 'REQUEST_ERROR', message: caught.message || '요청을 처리하지 못했습니다.', status: caught.status, details: caught.details || null };
      }
      // [추가] HTTP 상태가 있어도 프록시 통신 실패는 사전 거부로 단정하지 않는다.
      function unresolved(caught) {
        outcome.value = 'unknown';
        displayError({ code: caught.code || 'STREAM_INTERRUPTED', message: '연결이 중단되어 실행 완료 여부를 확인할 수 없습니다. 자동 재실행하지 않습니다.', details: { reason: caught.message, details: caught.details || null } });
      }
      function checkRecord(record) {
        if (!record || typeof record.workflow_id !== 'string' || typeof record.name !== 'string' || !Array.isArray(record.definition?.steps) || !/^[a-f0-9]{64}$/.test(record.definition_hash || '')) {
          throw Object.assign(new Error('서버에서 유효한 미리보기를 받지 못했습니다.'), { code: 'INVALID_RECORD' });
        }
        return record;
      }
      // [추가] 실행별 상태맵은 프로토타입을 두지 않아 특수 문자열 ID도 일반 키로만 저장한다.
      function clearRun() { timeline.value = []; states.value = Object.create(null); draft.value = ''; draftAttempt.value = null; result.value = null; terminal.value = null; outcome.value = 'idle'; }
      function select(record) { selected.value = checkRecord(record); saveName.value = record.name; clearRun(); }

      // [추가] 생성·로드·저장·실행은 한 번에 하나만 허용하고 모두 성공 이후에만 선택을 갱신한다.
      async function action(label, handler) {
        if (busy.value || disposed) return;
        busy.value = label; error.value = null; notice.value = '';
        const request = begin('action');
        try { await handler(request); }
        catch (caught) { if (current('action', request) && caught.name !== 'AbortError') displayError(caught); }
        finally { if (current('action', request)) busy.value = ''; }
      }
      async function refreshCatalog() {
        const request = begin('catalog'); catalogError.value = '';
        try {
          const data = await fetchJSON('/workflows/catalog', {}, request);
          if (!Array.isArray(data?.systems) || !Array.isArray(data?.incident_types)) throw new Error('카탈로그 형식이 올바르지 않습니다.');
          if (current('catalog', request)) catalog.value = data;
        } catch (caught) { if (current('catalog', request) && caught.name !== 'AbortError') catalogError.value = caught.message; }
      }
      async function refreshList(nextOffset = offset.value) {
        if (disposed) return;
        const request = begin('list'); listBusy.value = true; listError.value = '';
        try {
          const params = new URLSearchParams({ limit: '20', offset: String(nextOffset), include_drafts: String(includeDrafts.value) });
          const data = await fetchJSON(`/workflows?${params}`, {}, request);
          if (!Array.isArray(data?.workflows)) throw new Error('목록 응답 형식이 올바르지 않습니다.');
          if (current('list', request)) { rows.value = data.workflows; offset.value = nextOffset; }
        } catch (caught) { if (current('list', request) && caught.name !== 'AbortError') listError.value = caught.message; }
        finally { if (current('list', request)) listBusy.value = false; }
      }
      async function generate() {
        if (!query.value.trim() || query.value.length > 2000) return;
        await action('절차 생성 중', async request => {
          const data = await fetchJSON('/workflows/generate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ query: query.value }) }, request);
          if (current('action', request)) { select(data); notice.value = '절차를 생성했습니다. 계획을 확인한 뒤 직접 실행하세요. 아직 MCP 조회는 실행하지 않았습니다.'; }
        });
      }
      async function load(id) {
        await action('절차 불러오는 중', async request => {
          const data = await fetchJSON(`/workflows/${encodeURIComponent(id)}`, {}, request);
          if (current('action', request)) select(data);
        });
      }
      async function save() {
        if (!selected.value || !saveName.value.trim() || saveName.value.trim().length > 80) return;
        await action('이름 저장 중', async request => {
          const previous = selected.value;
          const data = checkRecord(await fetchJSON(`/workflows/${encodeURIComponent(previous.workflow_id)}/save`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: saveName.value }) }, request));
          if (data.definition_hash !== previous.definition_hash) throw Object.assign(new Error('저장 후 실행 해시가 바뀌었습니다. 절차를 다시 불러오세요.'), { code: 'DEFINITION_HASH_MISMATCH', status: 409 });
          if (current('action', request)) { selected.value = data; saveName.value = data.name; notice.value = '표시 이름과 저장 상태를 갱신했습니다. 실행 정의와 해시는 같습니다.'; void refreshList(0); }
        });
      }

      // [추가] 최종 이벤트의 result만 확정 보고로 사용하고 조회·보고 실패 자료도 그대로 보존한다.
      function applyTerminal(event) {
        terminal.value = event;
        // [추가] 프록시 연결 단절은 서버 실행의 실패 확정과 구분하고 기존 진행 기록을 유지한다.
        if (!event.result && transportCodes.has(event.error?.code)) {
          unresolved(event.error);
          return;
        }
        if (event.result) {
          result.value = event.result;
          for (const entry of event.result.execution?.trace || []) states.value[entry.node_id] = entry;
          if (event.result.draft) draft.value = event.result.draft;
        }
        outcome.value = event.type === 'FinalAnswer' && confirmed.value ? 'completed' : 'failed';
        if (outcome.value === 'failed') {
          const issue = event.error || event.result?.error || {};
          displayError({ code: issue.code || 'WORKFLOW_FAILED', message: issue.message || '실행이 실패했습니다. 미확정 초안을 최종 답변으로 사용하지 마세요.', details: event.result?.execution?.validation_errors });
        }
      }
      function onEvent(event) {
        if (event.type === 'AnswerToken') {
          if (draftAttempt.value !== event.attempt) { draft.value = ''; draftAttempt.value = event.attempt; }
          draft.value += event.delta || '';
          return;
        }
        timeline.value.push(event);
        if (event.node_id) states.value[event.node_id] = { ...event, status: event.type === 'ConditionEvaluated' ? 'completed' : event.status || 'started' };
        if (event.type === 'ReportStarted') { draftAttempt.value = event.attempt; draft.value = ''; }
        if (event.type === 'FinalAnswer' || event.type === 'WorkflowFailed') applyTerminal(event);
      }
      async function execute() {
        if (!canExecute.value) return;
        await action('실행 중', async request => {
          clearRun(); outcome.value = 'running';
          try {
            if (!window.WorkflowStream?.consume) throw Object.assign(new Error('진행 스트림 모듈이 준비되지 않았습니다. 화면을 새로고침하세요.'), { code: 'STREAM_MODULE_UNAVAILABLE', notStarted: true });
            const record = selected.value;
            const response = await fetch(`/workflows/${encodeURIComponent(record.workflow_id)}/execute/stream`, { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' }, body: JSON.stringify({ definition_hash: record.definition_hash }), signal: request.controller.signal });
            const ending = await window.WorkflowStream.consume(response, event => { if (current('action', request)) onEvent(event); });
            if (current('action', request) && !terminal.value) applyTerminal(ending);
          } catch (caught) {
            if (!current('action', request) || caught.name === 'AbortError') return;
            if (caught.terminal || terminal.value) {
              if (!terminal.value) applyTerminal(caught.terminal);
              notice.value = outcome.value === 'unknown'
                ? '기존 진행 기록을 유지했습니다. 연결 또는 종료 신호 확인에 문제가 있어 완료 여부는 알 수 없습니다.'
                : '서버가 확정한 결과를 유지했습니다. 이후 연결 또는 종료 신호 확인에 문제가 있었습니다.';
            } else if (caught.result?.status === 'failed') {
              // [추가] HTTP 오류로 전달된 전체 실패 결과도 완료 자료·미확정 초안과 함께 보존한다.
              const failure = { type: 'WorkflowFailed', result: caught.result, error: caught.result.error };
              timeline.value.push(failure); applyTerminal(failure);
            } else if (transportCodes.has(caught.code)) {
              unresolved(caught);
            } else if (caught.status >= 400 || caught.notStarted) {
              outcome.value = 'idle'; displayError(caught);
            } else {
              unresolved(caught);
            }
          }
        });
      }
      onMounted(() => { void refreshCatalog(); void refreshList(0); });
      // [추가] 탭 숨김은 실행을 유지하며 실제 컴포넌트 해제 때만 진행 중 요청을 정리한다.
      onBeforeUnmount(() => { disposed = true; for (const request of requests.values()) request.controller.abort(); requests.clear(); });
      return { query, examples, catalog, catalogError, listError, rows, offset, includeDrafts, listBusy, selected, saveName, busy, error, notice, timeline, states, draft, draftAttempt, result, outcome, fake, nodeCount, canExecute, observations, runbooks, confirmed, tokenNote, errorDetails, validationIssues, conflict, duration, outcomeText, valueText, conditionText, runbookTitle, eventText, refreshCatalog, refreshList, generate, load, save, execute };
    },
    template: `
      <section class="workflow-panel" aria-label="조건부 진단 워크플로" :aria-busy="!!busy">
        <div class="workflow-intro">
          <div>
            <h2>조건부 진단 워크플로</h2>
            <p>자연어로 절차를 만들고, 계획을 확인한 뒤 실행하세요.</p>
          </div>
          <span class="workflow-badge">순차 + if/else · 최대 6노드</span>
        </div>
        <p v-if="fake" class="workflow-notice">fake 모드는 아래에 등록된 예시 요청을 사용하세요. 자연어 이해 성능은 실제 모델에서 별도 확인합니다.</p>
        <div v-if="notice" class="workflow-notice" role="status">{{ notice }}</div>
        <div v-if="error" class="workflow-error" role="alert">
          <strong>{{ error.code }}</strong>
          <p>{{ error.message }}</p>
          <!-- [추가] 생성의 최종 code·path·message를 바로 표시하고 원본 상세 응답은 접힌 상태로 유지한다. -->
          <section v-if="validationIssues.length" aria-label="최종 검증 오류">
            <h4>최종 검증 오류</h4>
            <ul>
              <li v-for="(issue, index) in validationIssues" :key="index">
                <code>{{ issue.code }}</code> · <code>{{ issue.path }}</code>
                <p>{{ issue.message }}</p>
              </li>
            </ul>
          </section>
          <p v-if="conflict">미리보기와 현재 저장 상태를 다시 확인하세요. 카탈로그가 바뀌었다면 절차를 새로 생성해야 할 수 있습니다.</p>
          <button v-if="conflict && selected" type="button" :disabled="!!busy" @click="load(selected.workflow_id)">선택 절차 다시 불러오기</button>
          <details v-if="errorDetails">
            <summary>원본 상세 정보</summary>
            <pre>{{ errorDetails }}</pre>
          </details>
        </div>
        <div class="workflow-layout">
          <!-- [추가] 저장 목록·페이지 이동·자원 목록은 선택한 실행 계획과 별도로 관리한다. -->
          <aside class="workflow-sidebar workflow-box" aria-label="저장한 절차">
            <div class="workflow-section-heading">
              <h3>저장한 절차</h3>
              <button type="button" class="workflow-secondary" :disabled="!!busy || listBusy" @click="refreshList()">새로고침</button>
            </div>
            <label class="workflow-checkbox">
              <input type="checkbox" v-model="includeDrafts" :disabled="!!busy || listBusy" @change="refreshList(0)">
              초안도 표시
            </label>
            <p v-if="listError" class="workflow-error" role="alert">{{ listError }}</p>
            <p v-if="listBusy" class="workflow-muted" role="status">목록 불러오는 중…</p>
            <ul class="workflow-saved-list">
              <li v-for="row in rows" :key="row.workflow_id">
                <button type="button" :disabled="!!busy" :class="{ 'workflow-active': selected?.workflow_id === row.workflow_id }" @click="load(row.workflow_id)">
                  <strong>{{ row.name }}</strong>
                  <span>{{ row.status === 'saved' ? '저장됨' : '초안' }} · {{ row.created_at }}</span>
                </button>
              </li>
            </ul>
            <p v-if="!rows.length && !listBusy" class="workflow-muted">표시할 절차가 없습니다. 생성 후 이름을 저장하면 여기에 나타납니다.</p>
            <div class="workflow-pagination">
              <button type="button" class="workflow-secondary" :disabled="!!busy || listBusy || offset === 0" @click="refreshList(Math.max(0, offset - 20))">이전</button>
              <span>{{ Math.floor(offset / 20) + 1 }}페이지</span>
              <button type="button" class="workflow-secondary" :disabled="!!busy || listBusy || rows.length < 20" @click="refreshList(offset + 20)">다음</button>
            </div>
            <details class="workflow-catalog">
              <summary>사용 가능한 자원</summary>
              <p v-if="catalogError" class="workflow-error">{{ catalogError }}</p>
              <template v-if="catalog">
                <h4>시스템</h4>
                <div class="workflow-tags">
                  <code v-for="system in catalog.systems" :key="system">{{ system }}</code>
                </div>
                <h4>런북</h4>
                <div class="workflow-tags">
                  <code v-for="incident in catalog.incident_types" :key="incident">{{ incident }}</code>
                </div>
                <h4>조건 필드</h4>
                <p v-for="(field, key) in catalog.condition_fields" :key="key">
                  <code>{{ key }}</code>
                  · {{ field.operators.join(' ') }}
                  <span v-if="field.values"> · {{ field.values.join(', ') }}</span>
                </p>
              </template>
              <button type="button" class="workflow-secondary" :disabled="!!busy" @click="refreshCatalog">자원 다시 조회</button>
            </details>
          </aside>
          <main class="workflow-main">
            <!-- [추가] 자연어 요청은 먼저 절차로 생성하며 이 영역에서는 실행을 시작하지 않는다. -->
            <form class="workflow-box workflow-generate" @submit.prevent="generate">
              <label for="workflow-query">
                <strong>어떤 순서와 조건으로 조사할까요?</strong>
              </label>
              <textarea id="workflow-query" v-model="query" maxlength="2000" rows="4" :disabled="!!busy" placeholder="시스템, 비교 기준과 양쪽 분기 동작을 입력하세요." aria-describedby="workflow-query-help"></textarea>
              <div class="workflow-section-heading">
                <small id="workflow-query-help">{{ query.length }}/2000자 · 생성 단계에서는 조회하지 않습니다.</small>
                <button type="submit" :disabled="!!busy || !query.trim() || query.length > 2000">{{ busy === '절차 생성 중' ? '절차 생성 중…' : '절차 생성 · 실행 안 함' }}</button>
              </div>
              <div class="workflow-examples">
                <span>예시 요청</span>
                <button v-for="(example, index) in examples" :key="example" type="button" class="workflow-secondary" :disabled="!!busy" @click="query = example">{{ index + 1 }}. {{ ['CPU 조건 분기', '두 시스템 순차 조회', '정상 상태 분기'][index] }}</button>
              </div>
            </form>
            <!-- [추가] 저장된 원문·정의·해시를 확인하고 표시 이름 저장과 명시적 실행을 제공한다. -->
            <section v-if="selected" class="workflow-box" aria-label="절차 미리보기">
              <div class="workflow-section-heading">
                <h3>{{ selected.name }}</h3>
                <span class="workflow-badge">{{ selected.status === 'saved' ? '저장됨' : '초안' }} · {{ nodeCount }}/6노드</span>
              </div>
              <p class="workflow-query-original">원문: {{ selected.query }}</p>
              <details class="workflow-identity">
                <summary>정의 식별 정보</summary>
                <p>
                  ID:
                  <code>{{ selected.workflow_id }}</code>
                </p>
                <p>
                  실행 해시:
                  <code>{{ selected.definition_hash }}</code>
                </p>
                <p>분기 양쪽을 포함해 노드 수를 계산합니다. 저장된 절차도 실행할 때 새 수치를 조회합니다.</p>
              </details>
              <ol class="workflow-plan">
                <li v-for="step in selected.definition.steps" :key="step.id">
                  <workflow-step-card :step="step" :states="states" />
                </li>
              </ol>
              <form class="workflow-save" @submit.prevent="save">
                <label for="workflow-name">
                  재사용할 표시 이름
                  <input id="workflow-name" v-model="saveName" maxlength="80" :disabled="!!busy" placeholder="80자 이내">
                </label>
                <button type="submit" class="workflow-secondary" :disabled="!!busy || !saveName.trim() || saveName.trim().length > 80">이름 저장·재사용 등록</button>
              </form>
              <div class="workflow-execute">
                <p>표시 이름 저장은 실행 정의를 바꾸지 않습니다. 실행은 아래 버튼으로 한 번 시작합니다.</p>
                <button type="button" :disabled="!canExecute" @click="execute">{{ busy === '실행 중' ? '실행 중…' : '이 계획 실행' }}</button>
              </div>
              <p v-if="selected.compilation" class="workflow-muted">
                생성 모델 {{ selected.compilation.attempts.length }}회 · 생성 토큰 {{ valueText(selected.compilation.total_tokens) }}
                <span v-if="selected.compilation.attempts.some(item => item.usage?.measurement === 'estimated')">(추정치)</span>
              </p>
            </section>
            <section v-else class="workflow-box workflow-empty">
              <h3>생성하거나 저장한 절차를 선택하세요.</h3>
              <p>모니터링·런북 조회와 조건 분기를 실행 전에 확인할 수 있습니다.</p>
            </section>
            <!-- [추가] 실행 기록과 미확정 초안·확정 보고를 구분해 진행 및 실패 상태를 전달한다. -->
            <section v-if="outcome !== 'idle' || timeline.length" class="workflow-box" aria-label="실행 결과">
              <div class="workflow-section-heading">
                <h3>실행 기록</h3>
                <strong class="workflow-outcome" :class="'workflow-outcome-' + outcome" role="status" aria-live="polite">{{ outcomeText }}</strong>
              </div>
              <ol class="workflow-timeline">
                <li v-for="(event, index) in timeline" :key="index" :class="{ 'workflow-muted': event.type === 'StepSkipped', 'workflow-text-error': event.type === 'StepFailed' || event.type === 'WorkflowFailed' }">
                  <span>{{ eventText(event) }}</span>
                </li>
              </ol>
              <details v-if="draft && !confirmed" :open="outcome === 'running'" class="workflow-draft">
                <summary>
                  {{ outcome === 'running' ? '작성 중 초안 · 아직 확정되지 않음' : '미확정 초안 · 최종 답변이 아님' }}
                  <span v-if="draftAttempt"> · {{ draftAttempt }}차</span>
                </summary>
                <pre>{{ draft }}</pre>
              </details>
              <div v-if="confirmed" class="workflow-final">
                <h4>확정 보고</h4>
                <pre>{{ result.final }}</pre>
                <p class="workflow-muted">관측값·조건·조회 횟수는 실행 기록에서 작성했습니다. 모델이 생성한 런북 요약은 함께 표시한 근거 원문과 확인하세요.</p>
              </div>
              <!-- [추가] 요약 실패를 성공으로 바꾸지 않으면서 확인된 조회 사실은 계속 제공한다. -->
              <div v-else-if="result?.facts_report" class="workflow-final">
                <h4>확인된 조회 결과</h4>
                <p class="workflow-muted">최종 보고는 확정되지 않았습니다. 아래는 조회에 성공한 관측값과 실행 기록입니다.</p>
                <pre>{{ result.facts_report }}</pre>
              </div>
              <!-- [추가] 최종 결과에 포함된 사용량·관측값·실제 읽은 문서를 확인한다. -->
              <template v-if="result">
                <dl class="workflow-metadata">
                  <div>
                    <dt>실행 시간</dt>
                    <dd>{{ duration }}</dd>
                  </div>
                  <div>
                    <dt>보고 입력 / 출력 / 전체 토큰</dt>
                    <dd>
                      {{ valueText(result.input_tokens) }} / {{ valueText(result.output_tokens) }} / {{ valueText(result.total_tokens) }}
                      <small>{{ tokenNote }}</small>
                    </dd>
                  </div>
                  <div>
                    <dt>실행 ID</dt>
                    <dd>
                      <code :title="result.run_id">{{ result.run_id?.slice(0, 12) }}…</code>
                    </dd>
                  </div>
                </dl>
                <h4>이번 실행에서 수집한 자료</h4>
                <div v-if="observations.length" class="workflow-table-wrap">
                  <table class="workflow-table">
                    <caption>모니터링 관측값</caption>
                    <thead>
                      <tr>
                        <th>시스템</th>
                        <th>상태</th>
                        <th>CPU</th>
                        <th>메모리</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr v-for="item in observations" :key="item.node_id">
                        <td>{{ item.data.system_name }}</td>
                        <td>{{ item.data.status }}</td>
                        <td>{{ valueText(item.data.cpu_percent) }}%</td>
                        <td>{{ valueText(item.data.memory_percent) }}%</td>
                      </tr>
                    </tbody>
                  </table>
                </div>
                <div v-for="item in observations" :key="'alerts-' + item.node_id" class="workflow-observation">
                  <strong>{{ item.data.system_name }}</strong>
                  <small>조회 시각: {{ item.data.last_checked }}</small>
                  <ul>
                    <li v-for="(alert, index) in item.data.alerts" :key="index">{{ alert }}</li>
                    <li v-if="!item.data.alerts?.length" class="workflow-muted">알림 없음</li>
                  </ul>
                </div>
                <p v-if="!observations.length" class="workflow-muted">수집된 모니터링 자료가 없습니다.</p>
                <details v-for="item in runbooks" :key="item.node_id" class="workflow-runbook">
                  <summary>읽은 런북: {{ runbookTitle(item.data) }}</summary>
                  <pre>{{ item.data }}</pre>
                </details>
                <p v-if="!runbooks.length" class="workflow-muted">이번 실행에서 읽은 런북이 없습니다.</p>
              </template>
            </section>
          </main>
        </div>
        <p class="workflow-sr-only" role="status" aria-live="polite">{{ busy }}</p>
      </section>`,
  };
  window.WorkflowUI = { ...(window.WorkflowUI || {}), WorkflowPanel };
}());
