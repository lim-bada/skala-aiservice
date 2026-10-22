// [추가] 새 파일 전체: 브라우저 SSE 소비의 바이트 경계·프로토콜·오류 보존·자원 해제를 Node에서 검증한다.
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { consume, MAX_BUFFER_BYTES } = require("../web/workflow_stream.js");
const encoder = new TextEncoder();

// [추가] UI가 실제로 받는 전체 성공 결과와 terminal 이벤트를 독립적으로 만든다.
function completedEvent() {
  const result = {
    status: "completed", run_id: "실행-1", definition_hash: "a".repeat(64),
    report_valid: true, final: "1) 관측 결과\n2) 조건 판단\n3) 조치 후 확인",
    draft: "검토 완료", execution: { status: "completed", results: [] },
    report_attempts: [], elapsed_ms: 5, events: [],
  };
  return {
    type: "FinalAnswer", workflow_id: "절차-1", run_id: result.run_id,
    definition_hash: result.definition_hash, status: "completed", report_valid: true,
    final: result.final, result,
  };
}

// [추가] 서버가 실행 실패나 예외를 처리할 때 제공하는 최소 실패 이벤트를 만든다.
function failedEvent(withResult = false) {
  const event = {
    type: "WorkflowFailed", workflow_id: "절차-1", definition_hash: "b".repeat(64),
    status: "failed", report_valid: false,
    error: { code: "WORKFLOW_STREAM_ERROR", message: "진행 스트림을 처리하지 못했습니다." },
  };
  if (withResult) {
    event.run_id = "실패-실행";
    event.result = {
      status: "failed", run_id: event.run_id, report_valid: false, final: "", draft: "검토할 초안",
      error: { code: "WORKFLOW_TIMEOUT", stage: "report", message: "제한 시간 초과" },
      execution: { status: "completed", results: [] }, report_attempts: [], events: [], elapsed_ms: 10,
    };
  }
  return event;
}

// [추가] 프레임 구분자를 바꿔 동일 이벤트를 LF·CRLF·CR 스트림으로 구성한다.
function frame(event, newline = "\n") {
  return `data: ${JSON.stringify(event)}${newline}${newline}`;
}

// [추가] 실제 Web Response·ReadableStream으로 임의의 UTF-8 조각을 전달한다.
function responseFromChunks(chunks, options = {}) {
  const body = new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(typeof chunk === "string" ? encoder.encode(chunk) : chunk);
      controller.close();
    },
  });
  return new Response(body, { headers: { "Content-Type": "text/event-stream; charset=utf-8" }, ...options });
}

// [추가] 실제 reader 동작은 유지하면서 취소·잠금 해제·연결 오류를 정확히 관찰한다.
function instrumentedResponse(chunks, { readError = null, errorAt = null, cancelError = null, releaseError = null } = {}) {
  const state = { reads: 0, cancels: 0, releases: 0 };
  let index = 0;
  const reader = {
    async read() {
      state.reads += 1;
      if (readError && index === errorAt) throw readError;
      if (index >= chunks.length) return { done: true };
      const value = chunks[index++];
      return { done: false, value: typeof value === "string" ? encoder.encode(value) : value };
    },
    async cancel() {
      state.cancels += 1;
      if (cancelError) throw cancelError;
    },
    releaseLock() {
      state.releases += 1;
      if (releaseError) throw releaseError;
    },
  };
  return {
    state,
    response: {
      ok: true, status: 200, headers: new Headers({ "Content-Type": "text/event-stream" }),
      body: { getReader: () => reader },
    },
  };
}

// [추가] 프로토콜 오류를 검사할 때 완료 결과를 성공으로 반환하지 않는지 공통 확인한다.
async function rejectsProtocol(response, callback) {
  let found;
  await assert.rejects(consume(response, callback), (error) => {
    found = error;
    assert.equal(error.code, "STREAM_PROTOCOL_ERROR");
    return true;
  });
  return found;
}

// [추가] CommonJS와 일반 브라우저 스크립트가 같은 consume API를 노출한다.
test("CommonJS와 브라우저 global 모두 consume을 노출한다", () => {
  assert.equal(typeof consume, "function");
  const source = fs.readFileSync(path.join(__dirname, "../web/workflow_stream.js"), "utf8");
  const browser = {};
  browser.window = browser;
  vm.runInNewContext(source, browser);
  assert.equal(typeof browser.WorkflowStream.consume, "function");
  assert.equal(browser.window.WorkflowStream, browser.WorkflowStream);
});

// [추가] 진행 이벤트는 순서대로 전달하고 DONE까지 확인한 terminal만 반환한다.
test("합쳐진 여러 프레임의 진행·terminal·DONE을 처리한다", async () => {
  const terminal = completedEvent();
  const progress = { type: "StepStarted", node_id: "metrics_1", message: "결제 상태 조회 중" };
  const response = responseFromChunks([frame(progress) + frame(terminal) + "data: [DONE]\n\n"]);
  const received = [];
  const result = await consume(response, (event) => received.push(event));
  assert.deepEqual(received, [progress, terminal]);
  assert.deepEqual(result, terminal);
  assert.equal(response.body.locked, false);
});

// [추가] 모든 바이트 절단 위치에서 한글·이모지·JSON이 손실 없이 복원되는지 확인한다.
test("한국어와 이모지의 모든 UTF-8 청크 경계를 복원한다", async () => {
  const terminal = completedEvent();
  const progress = { type: "AnswerToken", delta: "결제 장애 확인 중입니다 🔎", attempt: 1 };
  const bytes = encoder.encode(frame(progress) + frame(terminal) + "data: [DONE]\n\n");
  for (let cut = 1; cut < bytes.length; cut += 1) {
    const response = responseFromChunks([bytes.slice(0, cut), bytes.slice(cut)]);
    const received = [];
    const result = await consume(response, (event) => received.push(event));
    assert.equal(received[0].delta, progress.delta, `바이트 경계 ${cut}`);
    assert.deepEqual(result, terminal);
  }
});

// [추가] CRLF가 서로 다른 청크에 나뉘거나 CR만 있어도 같은 프레임으로 읽는다.
test("LF·CRLF·CR을 한 바이트씩 받아도 프레임을 복원한다", async () => {
  for (const newline of ["\n", "\r\n", "\r"]) {
    const terminal = completedEvent();
    const bytes = encoder.encode(frame(terminal, newline) + `data: [DONE]${newline}${newline}`);
    const response = responseFromChunks([...bytes].map((byte) => new Uint8Array([byte])));
    assert.deepEqual(await consume(response), terminal);
  }
});

// [추가] 여러 data 줄은 개행으로 합치고 주석·id·event·retry 필드는 동작을 만들지 않는다.
test("주석과 여러 data 줄을 포함한 프레임을 처리한다", async () => {
  const terminal = completedEvent();
  const multiline = JSON.stringify(terminal, null, 2).split("\n").map((line) => `data: ${line}`).join("\r\n");
  const text = ": 연결 유지\r\nid: 42\r\nevent: custom\r\nretry: 1\r\n\r\n" + multiline + "\r\n\r\ndata: [DONE]\r\n\r\n";
  const received = [];
  assert.deepEqual(await consume(responseFromChunks([text]), (event) => received.push(event)), terminal);
  assert.equal(received.length, 1);
});

// [추가] 최소 실패 이벤트는 전체 result가 없어도 원래 오류와 식별자를 보존한다.
test("전체 결과 없는 WorkflowFailed도 DONE과 함께 반환한다", async () => {
  const terminal = failedEvent();
  const result = await consume(responseFromChunks([frame(terminal), "data: [DONE]\n\n"]));
  assert.deepEqual(result, terminal);
  assert.equal(result.error.code, "WORKFLOW_STREAM_ERROR");
});

// [추가] 정상 실패 경로의 전체 조회·초안·오류 결과도 성공처럼 그대로 보존한다.
test("실패 terminal의 전체 RunResult를 보존한다", async () => {
  const terminal = failedEvent(true);
  assert.deepEqual(await consume(responseFromChunks([frame(terminal) + "data: [DONE]\n\n"])), terminal);
});

// [추가] 잘못된 JSON·배열·스칼라·type 없는 객체를 진행 이벤트로 채택하지 않는다.
test("JSON이나 객체 계약이 잘못되면 프로토콜 오류를 반환한다", async () => {
  for (const payload of ["{bad", "[]", "null", "1", '"text"', "{}", '{"type":12}', '{"type":" "}']) {
    await rejectsProtocol(responseFromChunks([`data: ${payload}\n\n`]));
  }
});

// [추가] 성공 terminal에는 검증된 전체 결과가 필요하다.
test("결과가 없거나 실패 상태인 FinalAnswer를 거부한다", async () => {
  for (const event of [
    { type: "FinalAnswer" },
    { ...completedEvent(), result: null },
    { ...completedEvent(), result: {} },
    { ...completedEvent(), result: { status: "failed", report_valid: false, final: "" } },
    { ...completedEvent(), report_valid: false },
  ]) {
    await rejectsProtocol(responseFromChunks([frame(event) + "data: [DONE]\n\n"]));
  }
});

// [추가] 실패 terminal도 구조화 오류 없이 성공 판정을 흉내낼 수 없다.
test("오류 정보나 상태가 잘못된 WorkflowFailed를 거부한다", async () => {
  for (const event of [
    { type: "WorkflowFailed" },
    { ...failedEvent(), error: "failed" },
    { ...failedEvent(), error: { code: "X" } },
    { ...failedEvent(), result: {} },
    { ...failedEvent(), status: "completed" },
  ]) {
    await rejectsProtocol(responseFromChunks([frame(event) + "data: [DONE]\n\n"]));
  }
});

// [추가] DONE이나 EOF만으로 완료했다고 표시하지 않는다.
test("terminal 없는 DONE과 완료 없는 EOF는 실패한다", async () => {
  for (const chunks of [["data: [DONE]\n\n"], [], [frame({ type: "StepStarted" })], ["data: [DONE]"]]) {
    await rejectsProtocol(responseFromChunks(chunks));
  }
});

// [추가] terminal 이후 EOF도 연결 완료로 오인하지 않으며 확인된 결과를 오류에 붙인다.
test("terminal 뒤 DONE 없는 EOF는 error.terminal에 결과를 보존한다", async () => {
  const terminal = completedEvent();
  const error = await rejectsProtocol(responseFromChunks([frame(terminal)]));
  assert.deepEqual(error.terminal, terminal);
});

// [추가] 두 번째 terminal이나 뒤늦은 진행 이벤트가 처음 확정된 결과를 덮지 못한다.
test("중복 terminal과 terminal 뒤 진행 이벤트를 거부하고 첫 결과를 보존한다", async () => {
  for (const later of [failedEvent(), completedEvent(), { type: "AnswerToken", delta: "뒤늦은 내용" }]) {
    const terminal = completedEvent();
    const received = [];
    const error = await rejectsProtocol(responseFromChunks([frame(terminal) + frame(later) + "data: [DONE]\n\n"]), (event) => received.push(event));
    assert.deepEqual(error.terminal, terminal);
    assert.deepEqual(received, [terminal]);
  }
});

// [추가] 같은 청크의 DONE 이후 데이터도 확정 결과 변경 없이 오류로 알린다.
test("DONE 이후 추가 data를 거부한다", async () => {
  const terminal = completedEvent();
  const error = await rejectsProtocol(responseFromChunks([frame(terminal) + "data: [DONE]\n\n" + frame(failedEvent())]));
  assert.deepEqual(error.terminal, terminal);
});

// [추가] 한글의 잘린 바이트나 잘못된 UTF-8을 대체문자로 숨기지 않는다.
test("잘린 UTF-8과 잘못된 바이트를 거부한다", async () => {
  for (const bytes of [new Uint8Array([0xf0, 0x9f]), new Uint8Array([0xff]), new Uint8Array([0xc0, 0xaf])]) {
    await rejectsProtocol(responseFromChunks([bytes]));
  }
});

// [추가] 확정 결과 뒤의 잘린 UTF-8도 결과를 버리지 않고 연결 오류로 구분한다.
test("terminal 뒤 UTF-8 오류는 확정 결과를 유지한다", async () => {
  const terminal = completedEvent();
  const error = await rejectsProtocol(responseFromChunks([frame(terminal), new Uint8Array([0xe3, 0x81])]));
  assert.deepEqual(error.terminal, terminal);
});

// [추가] 한 줄과 여러 data 줄의 누적 버퍼를 모두 1MiB 이하로 제한한다.
test("미완성 프레임의 1MiB 버퍼 초과를 차단한다", async () => {
  const oversized = "data: " + "a".repeat(MAX_BUFFER_BYTES + 1);
  await rejectsProtocol(responseFromChunks([oversized]));
  const half = "a".repeat(Math.ceil(MAX_BUFFER_BYTES / 2));
  await rejectsProtocol(responseFromChunks([`data: ${half}\ndata: ${half}`]));
});

// [추가] 버퍼 크기를 글자 수로 세어 한글에서 제한을 우회하지 않는다.
test("한글 프레임도 UTF-8 바이트 크기로 제한한다", async () => {
  const large = "data: " + "가".repeat(Math.floor(MAX_BUFFER_BYTES / 3) + 1);
  assert.ok(large.length < MAX_BUFFER_BYTES);
  await rejectsProtocol(responseFromChunks([large]));
});

// [추가] JSON 서비스 오류의 상태·코드·설명·상세 자료를 잃지 않는다.
test("HTTP 409 서비스 오류 JSON을 보존한다", async () => {
  const details = { errors: [{ code: "UNKNOWN_SYSTEM", path: "steps[0].system" }] };
  const response = new Response(JSON.stringify({ error: { code: "CATALOG_CHANGED", message: "자원이 변경됐습니다.", details } }), {
    status: 409, headers: { "Content-Type": "application/json" },
  });
  await assert.rejects(consume(response), (error) => {
    assert.equal(error.status, 409);
    assert.equal(error.code, "CATALOG_CHANGED");
    assert.equal(error.message, "자원이 변경됐습니다.");
    assert.deepEqual(error.details, details);
    return true;
  });
});

// [추가] JSON 실행 API의 502 실패 응답도 전체 RunResult를 error.result로 제공한다.
test("HTTP 502 RunResult의 전체 결과를 보존한다", async () => {
  const result = failedEvent(true).result;
  const response = new Response(JSON.stringify(result), { status: 502, headers: { "Content-Type": "application/json" } });
  await assert.rejects(consume(response), (error) => {
    assert.equal(error.status, 502);
    assert.equal(error.code, "WORKFLOW_TIMEOUT");
    assert.equal(error.message, "제한 시간 초과");
    assert.deepEqual(error.result, result);
    return true;
  });
});

// [추가] 오류 envelope가 명시적으로 제공하는 result도 유지한다.
test("HTTP 오류 envelope의 details와 result를 함께 보존한다", async () => {
  const result = failedEvent(true).result;
  const response = new Response(JSON.stringify({ error: { code: "MODEL_ERROR", message: "보고 실패", details: null }, result }), {
    status: 502, headers: { "Content-Type": "application/json" },
  });
  await assert.rejects(consume(response), (error) => {
    assert.equal(error.details, null);
    assert.deepEqual(error.result, result);
    return true;
  });
});

// [추가] HTML 오류나 빈 응답도 파싱 오류 대신 원래 HTTP 상태를 전달한다.
test("JSON이 아닌 HTTP 오류에서 원래 status를 보존한다", async () => {
  const response = new Response("<html>오류</html>", { status: 503, headers: { "Content-Type": "text/html" } });
  await assert.rejects(consume(response), (error) => {
    assert.equal(error.status, 503);
    assert.equal(error.code, "HTTP_ERROR");
    return true;
  });
});

// [추가] 200이어도 SSE가 아니거나 본문이 없으면 진행 스트림으로 읽지 않는다.
test("잘못된 content-type과 빈 body를 거부한다", async () => {
  for (const response of [
    new Response("{}", { headers: { "Content-Type": "application/json" } }),
    new Response("data: [DONE]\n\n", { headers: { "Content-Type": "text/event-streamish" } }),
    new Response(null, { headers: { "Content-Type": "text/event-stream" } }),
  ]) {
    await rejectsProtocol(response);
  }
});

// [추가] content-type의 대소문자와 매개변수는 유효한 SSE를 거부하는 원인이 되지 않는다.
test("대소문자와 charset이 있는 SSE content-type을 허용한다", async () => {
  const terminal = completedEvent();
  const response = responseFromChunks([frame(terminal) + "data: [DONE]\n\n"], {
    headers: { "Content-Type": "Text/Event-Stream; Charset=UTF-8" },
  });
  assert.deepEqual(await consume(response), terminal);
});

// [추가] 정상 완료 후에도 추가 read 없이 cancel과 releaseLock을 수행한다.
test("성공하면 reader를 취소·해제하고 DONE 뒤 다시 읽지 않는다", async () => {
  const terminal = completedEvent();
  const fixture = instrumentedResponse([frame(terminal), "data: [DONE]\n\n", "추가 읽기 금지"]);
  assert.deepEqual(await consume(fixture.response), terminal);
  assert.deepEqual(fixture.state, { reads: 2, cancels: 1, releases: 1 });
});

// [추가] 연결 오류 이후에도 한 번의 취소·해제를 보장하고 확정 결과를 유지한다.
test("terminal 뒤 연결 오류는 원래 Error와 결과를 보존한다", async () => {
  const terminal = completedEvent();
  const failure = new Error("연결이 끊겼습니다.");
  failure.code = "ECONNRESET";
  const fixture = instrumentedResponse([frame(terminal)], { readError: failure, errorAt: 1 });
  await assert.rejects(consume(fixture.response), (error) => {
    assert.equal(error, failure);
    assert.equal(error.code, "ECONNRESET");
    assert.deepEqual(error.terminal, terminal);
    return true;
  });
  assert.equal(fixture.state.cancels, 1);
  assert.equal(fixture.state.releases, 1);
});

// [추가] 동기·비동기 콜백 실패도 정리 절차를 건너뛰지 않는다.
test("콜백 실패와 AbortError에서도 reader를 정리한다", async () => {
  for (const asyncCallback of [false, true]) {
    const failure = new Error("화면 처리 실패");
    const fixture = instrumentedResponse([frame({ type: "StepStarted" })]);
    const callback = asyncCallback ? async () => { throw failure; } : () => { throw failure; };
    await assert.rejects(consume(fixture.response, callback), (error) => error === failure);
    assert.equal(fixture.state.cancels, 1);
    assert.equal(fixture.state.releases, 1);
  }
  const abort = new DOMException("중단됐습니다.", "AbortError");
  const fixture = instrumentedResponse([], { readError: abort, errorAt: 0 });
  await assert.rejects(consume(fixture.response), (error) => error === abort);
  assert.equal(fixture.state.cancels, 1);
  assert.equal(fixture.state.releases, 1);
});

// [추가] 정리 코드 자체가 실패해도 원래 프로토콜 오류나 성공 결과를 덮지 않는다.
test("cancel·releaseLock 오류가 최초 결과를 덮지 않는다", async () => {
  const terminal = completedEvent();
  const success = instrumentedResponse([frame(terminal) + "data: [DONE]\n\n"], {
    cancelError: new Error("cancel 오류"), releaseError: new Error("release 오류"),
  });
  assert.deepEqual(await consume(success.response), terminal);
  const failure = instrumentedResponse(["data: broken\n\n"], { cancelError: new Error("cancel 오류") });
  await rejectsProtocol(failure.response);
  assert.equal(failure.state.releases, 1);
});

// [추가] 콜백이 terminal 객체를 변경해도 내부에서 확인한 결과는 그대로 반환한다.
test("terminal 콜백 변조가 반환 결과에 영향을 주지 않는다", async () => {
  const terminal = completedEvent();
  const result = await consume(responseFromChunks([frame(terminal) + "data: [DONE]\n\n"]), (event) => {
    event.type = "변경";
    event.result.final = "변경된 본문";
  });
  assert.deepEqual(result, terminal);
});

// [추가] 파서가 네트워크 재호출이나 자동 재접속을 시도하지 않는다.
test("연결 실패 시 fetch나 POST를 다시 실행하지 않는다", async () => {
  const original = globalThis.fetch;
  let fetches = 0;
  globalThis.fetch = async () => { fetches += 1; throw new Error("재호출 금지"); };
  try {
    await rejectsProtocol(responseFromChunks([frame({ type: "StepStarted" })]));
    assert.equal(fetches, 0);
  } finally {
    globalThis.fetch = original;
  }
});
