// [추가] 새 파일 전체: POST 응답의 SSE를 소비하며 완료 신호·확정 결과·연결 오류를 구분한다.
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.WorkflowStream = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // [추가] 한 프레임의 미완성 줄과 누적 data가 메모리를 무한히 차지하지 않게 제한한다.
  const MAX_BUFFER_BYTES = 1024 * 1024;
  const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
  const hasOwn = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
  const clone = (value) => JSON.parse(JSON.stringify(value));

  // [추가] 파싱·완료 계약 오류를 HTTP·모델 실패와 구분한다.
  function protocolError(message) {
    const error = new Error(message);
    error.code = "STREAM_PROTOCOL_ERROR";
    return error;
  }

  // [추가] 결과가 이미 확정됐다면 이후 연결·콜백 오류에도 그 결과를 잃지 않는다.
  function preserveTerminal(cause, terminal) {
    let error = cause instanceof Error ? cause : new Error("진행 이벤트를 처리하지 못했습니다.", { cause });
    if (terminal) {
      try {
        error.terminal = clone(terminal);
      } catch (_) {
        const wrapped = new Error(error.message, { cause: error });
        for (const key of ["code", "status", "details", "result"]) {
          if (hasOwn(error, key)) wrapped[key] = error[key];
        }
        wrapped.terminal = clone(terminal);
        error = wrapped;
      }
    }
    return error;
  }

  // [추가] 서비스 오류 JSON과 실행 실패 RunResult JSON을 모두 보존하는 오류를 만든다.
  async function responseError(response, wrongContentType) {
    let payload = null;
    try {
      payload = await response.json();
    } catch (_) {
      // [추가] HTML·빈 응답·깨진 JSON에서도 원래 HTTP 상태를 안내할 수 있게 한다.
    }
    const detail = isObject(payload) && isObject(payload.error) ? payload.error : payload;
    const message = isObject(detail) && typeof detail.message === "string" && detail.message
      ? detail.message
      : wrongContentType ? "진행 응답의 형식이 SSE가 아닙니다." : "워크플로 요청에 실패했습니다.";
    const error = new Error(message);
    error.status = response.status;
    error.code = isObject(detail) && typeof detail.code === "string" && detail.code
      ? detail.code : wrongContentType ? "STREAM_PROTOCOL_ERROR" : "HTTP_ERROR";
    if (isObject(detail) && hasOwn(detail, "details")) error.details = detail.details;
    else if (isObject(payload) && hasOwn(payload, "details")) error.details = payload.details;
    if (isObject(payload) && hasOwn(payload, "result")) error.result = payload.result;
    else if (isObject(detail) && hasOwn(detail, "result")) error.result = detail.result;
    else if (isObject(payload) && typeof payload.run_id === "string" && ["completed", "failed"].includes(payload.status)) {
      error.result = payload;
    }
    return error;
  }

  // [추가] UTF-16 문자열 길이가 아닌 UTF-8 바이트 수로 누적 메모리를 센다.
  function characterBytes(character) {
    const point = character.codePointAt(0);
    return point <= 0x7f ? 1 : point <= 0x7ff ? 2 : point <= 0xffff ? 3 : 4;
  }

  // [추가] 네트워크 청크와 무관하게 LF·CRLF·CR 및 여러 data 줄을 SSE 프레임으로 나눈다.
  class SSEParser {
    constructor() {
      this.line = "";
      this.lineBytes = 0;
      this.data = [];
      this.dataBytes = 0;
      this.skipLF = false;
    }

    // [추가] 프레임을 하나씩 yield하여 같은 청크 안에서도 콜백 순서·실패 위치를 보존한다.
    *push(text) {
      for (const character of text) {
        if (this.skipLF) {
          this.skipLF = false;
          if (character === "\n") continue;
        }
        if (character === "\r" || character === "\n") {
          this.skipLF = character === "\r";
          const line = this.line;
          const lineBytes = this.lineBytes;
          this.line = "";
          this.lineBytes = 0;
          if (line === "") {
            if (this.data.length) {
              const frame = this.data.join("\n");
              this.data = [];
              this.dataBytes = 0;
              yield frame;
            }
          } else if (line === "data" || line.startsWith("data:")) {
            let value = line === "data" ? "" : line.slice(5);
            let valueBytes = line === "data" ? 0 : lineBytes - 5;
            if (value.startsWith(" ")) {
              value = value.slice(1);
              valueBytes -= 1;
            }
            this.dataBytes += valueBytes + (this.data.length ? 1 : 0);
            if (this.dataBytes > MAX_BUFFER_BYTES) throw protocolError("진행 이벤트의 버퍼 제한을 초과했습니다.");
            this.data.push(value);
          }
          // [추가] 주석·id·event·retry 등은 상태나 재접속 동작을 만들지 않고 무시한다.
        } else {
          this.lineBytes += characterBytes(character);
          if (this.lineBytes + this.dataBytes > MAX_BUFFER_BYTES) {
            throw protocolError("진행 이벤트의 버퍼 제한을 초과했습니다.");
          }
          this.line += character;
        }
      }
    }
  }

  // [추가] 최종 성공은 전체 결과가 있어야 하며, 스트림 처리 실패는 최소 오류 이벤트도 받는다.
  function validateTerminal(event) {
    if (event.type === "FinalAnswer") {
      const result = event.result;
      if (!isObject(result) || result.status !== "completed" || result.report_valid !== true
          || typeof result.final !== "string" || !result.final.trim()) {
        throw protocolError("완료 이벤트에 검증된 전체 결과가 없습니다.");
      }
      if ((hasOwn(event, "status") && event.status !== "completed")
          || (hasOwn(event, "report_valid") && event.report_valid !== true)) {
        throw protocolError("완료 이벤트의 상태가 결과와 다릅니다.");
      }
    } else {
      const result = event.result;
      const issue = isObject(event.error) ? event.error : isObject(result) ? result.error : null;
      if (!isObject(issue) || typeof issue.code !== "string" || !issue.code.trim()
          || typeof issue.message !== "string" || !issue.message.trim()) {
        throw protocolError("실패 이벤트에 오류 정보가 없습니다.");
      }
      if (hasOwn(event, "result") && (!isObject(result) || result.status !== "failed" || result.report_valid !== false || result.final !== "")) {
        throw protocolError("실패 이벤트의 전체 결과가 올바르지 않습니다.");
      }
      if ((hasOwn(event, "status") && event.status !== "failed")
          || (hasOwn(event, "report_valid") && event.report_valid !== false)) {
        throw protocolError("실패 이벤트의 상태가 결과와 다릅니다.");
      }
    }
  }

  // [추가] POST 응답을 한 번만 소비한다. DONE 전 연결 오류는 확정 결과를 보존한 채 실패로 알린다.
  async function consume(response, onEvent = () => {}) {
    if (!response || typeof response.ok !== "boolean" || !response.headers || typeof response.headers.get !== "function") {
      throw protocolError("유효한 HTTP 응답이 필요합니다.");
    }
    const contentType = (response.headers.get("content-type") || "").split(";", 1)[0].trim().toLowerCase();
    if (!response.ok || contentType !== "text/event-stream") {
      throw await responseError(response, response.ok && contentType !== "text/event-stream");
    }
    if (typeof onEvent !== "function") throw new TypeError("이벤트 처리 함수가 필요합니다.");
    if (!response.body || typeof response.body.getReader !== "function") {
      throw protocolError("진행 응답의 본문이 없습니다.");
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8", { fatal: true });
    const parser = new SSEParser();
    let terminal = null;
    let doneReceived = false;

    // [추가] UTF-8이 잘렸거나 잘못된 바이트는 대체문자로 숨기지 않는다.
    function decode(bytes, stream) {
      try {
        return decoder.decode(bytes, { stream });
      } catch (_) {
        throw protocolError("진행 응답의 UTF-8 데이터가 올바르지 않습니다.");
      }
    }

    // [추가] JSON 객체와 단 하나의 terminal을 검증하며 콜백의 비동기 실패도 기다린다.
    async function dispatch(text) {
      for (const raw of parser.push(text)) {
        if (doneReceived) throw protocolError("완료 신호 이후에 추가 이벤트가 있습니다.");
        if (raw.trim() === "[DONE]") {
          if (!terminal) throw protocolError("최종 결과 없이 완료 신호를 받았습니다.");
          doneReceived = true;
          continue;
        }
        if (terminal) throw protocolError("최종 결과 이후에 추가 이벤트가 있습니다.");
        let event;
        try {
          event = JSON.parse(raw);
        } catch (_) {
          throw protocolError("진행 이벤트가 올바른 JSON이 아닙니다.");
        }
        if (!isObject(event) || typeof event.type !== "string" || !event.type.trim()) {
          throw protocolError("진행 이벤트는 type이 있는 JSON 객체여야 합니다.");
        }
        if (event.type === "FinalAnswer" || event.type === "WorkflowFailed") {
          validateTerminal(event);
          terminal = clone(event);
        }
        await onEvent(event);
      }
    }

    try {
      while (!doneReceived) {
        const item = await reader.read();
        if (item.done) {
          await dispatch(decode(undefined, false));
          if (!doneReceived) throw protocolError("완료 신호를 받기 전에 연결이 종료됐습니다.");
          break;
        }
        await dispatch(decode(item.value, true));
      }
      // [추가] DONE과 같은 청크에 잘린 UTF-8 바이트가 남은 경우도 오류로 구분한다.
      await dispatch(decode(undefined, false));
      return terminal;
    } catch (error) {
      throw preserveTerminal(error, terminal);
    } finally {
      // [추가] 성공·오류·콜백 실패·취소 모두 읽기 자원을 해제하며 정리 오류가 원인을 덮지 않게 한다.
      try {
        await reader.cancel();
      } catch (_) {
        // [추가] 이미 실패하거나 닫힌 스트림의 cancel 오류는 원래 결과를 유지한다.
      }
      try {
        reader.releaseLock();
      } catch (_) {
        // [추가] 잠금 해제 중 오류도 확정 결과나 최초 오류를 바꾸지 않는다.
      }
    }
  }

  return Object.freeze({ consume, MAX_BUFFER_BYTES });
});
