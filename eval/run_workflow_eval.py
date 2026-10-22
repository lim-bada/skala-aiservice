# [추가] 새 파일 전체: 같은 문항·고정 관측값으로 원본 v2와 조건부 워크플로를 비교하는 CLI다.
"""python -m eval.run_workflow_eval --backend fake --split all --reuse-first"""
import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from time import perf_counter

from dotenv import load_dotenv

from agent.workflow.catalog import load_catalog
from agent.workflow.models import MonitoringSnapshot
from agent.workflow.prompting import build_workflow_messages, workflow_response_schema
from agent.workflow.store import SqliteWorkflowStore
from agent.workflow.validator import validate_workflow
from eval.runtime import (
    ROOT, MeteredModel, fatal_provider_error, generate_saved, run_baseline,
    run_saved, run_guard_checks, usage_total,
)
from eval.scoring import score_calls, score_definition

CASE_PATH = ROOT / "eval/cases/workflow_cases.json"
FIXTURE_PATH = ROOT / "eval/fixtures/monitoring.json"


# [추가] 문항·정답·스냅샷의 오류는 모델 호출 전에 차단한다.
def load_cases(path=CASE_PATH, split="all", limit=None):
    raw = json.loads(Path(path).read_text())
    if raw.get("schema_version") != 1 or not isinstance(raw.get("cases"), list):
        raise ValueError("잘못된 평가 문항 형식")
    catalog = load_catalog()
    base = json.loads(FIXTURE_PATH.read_text())["snapshots"]
    if set(base) != set(catalog.systems):
        raise ValueError("고정 스냅샷과 실제 카탈로그가 다릅니다.")
    for system, snapshot in base.items():
        if MonitoringSnapshot.model_validate(snapshot).system_name != system:
            raise ValueError("스냅샷 시스템 이름 불일치")
    seen = set()
    for case in raw["cases"]:
        if case["id"] in seen or case["split"] not in ("dev", "holdout") or type(case["supported"]) is not bool:
            raise ValueError("중복 ID 또는 잘못된 문항 분류")
        seen.add(case["id"])
        if not isinstance(case["query"], str) or not case["query"].strip() or len(case["query"]) > 2000:
            raise ValueError("잘못된 평가 요청")
        if case["supported"] and not validate_workflow(case["expected_definition"], catalog).valid:
            raise ValueError("실행할 수 없는 정답 절차: " + case["id"])
        if not case["scenarios"]:
            raise ValueError("문항에는 고정 시나리오가 필요합니다.")
        scenario_ids = set()
        for scenario in case["scenarios"]:
            if scenario["id"] in scenario_ids:
                raise ValueError("중복 시나리오 ID")
            scenario_ids.add(scenario["id"])
            for system, snapshot in scenario["snapshots"].items():
                if system not in base or MonitoringSnapshot.model_validate(snapshot).system_name != system:
                    raise ValueError("시나리오 스냅샷 시스템 불일치")
            score_calls([], scenario["expected_calls"])
            if not case["supported"] and (case["expected_definition"] is not None or scenario["expected_calls"]):
                raise ValueError("미지원 정답은 실행 정의·조회 호출을 포함할 수 없습니다.")
    cases = [case for case in raw["cases"] if split == "all" or case["split"] == split]
    return cases[:limit] if limit is not None else cases


# [추가] 결과 생성 시점의 소스·문항·fixture·실행 환경을 해시로 식별한다.
def manifest(args, cases):
    files = sorted(list((ROOT / "agent").rglob("*.py")) + list((ROOT / "prompts").glob("*.txt")) + list((ROOT / "eval").glob("*.py")))
    source = hashlib.sha256()
    for path in files:
        source.update(str(path.relative_to(ROOT)).encode())
        source.update(path.read_bytes())
    packages = {}
    for name in ("httpx", "pydantic", "langgraph", "langsmith"):
        packages[name] = importlib.metadata.version(name)
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(), "backend": args.backend,
        "mode": args.mode, "report_mode": args.report_mode, "split": args.split,
        # [추가] 이미 확인한 문항으로 수정한 버전을 재측정할 때 독립 holdout으로 오해하지 않게 한다.
        "evaluation_role": getattr(args, "evaluation_role", "initial"),
        "selected_case_ids": [case["id"] for case in cases], "source_sha256": source.hexdigest(),
        "case_sha256": hashlib.sha256(CASE_PATH.read_bytes()).hexdigest(),
        "fixture_sha256": hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest(),
        "runbook_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted((ROOT / "data/runbooks").glob("*.md"))},
        "catalog_sha256": hashlib.sha256(json.dumps(load_catalog().to_dict(), sort_keys=True).encode()).hexdigest(),
        "python": platform.python_version(), "packages": packages,
        "models": {"fast": os.environ.get("OPENAI_FAST_MODEL", "gpt-4o-mini") if args.backend == "openai" else "fast-model-stub",
                   "quality": os.environ.get("OPENAI_QUALITY_MODEL", "gpt-4o") if args.backend == "openai" else "quality-model-stub"},
        "temperature": 0 if args.backend == "openai" else None,
        "workflow_output_limit": 4096, "workflow_max_compile_attempts": 2,
        "workflow_max_report_attempts": 3, "generation_timeout_seconds": args.timeout,
        "execution_timeout_seconds": args.timeout, "reuse_first": args.reuse_first,
        "fixture_transport": "in-process MCP-compatible fixture, no jitter, no forced failure, no network MCP",
        "baseline_fixed_delay_seconds_per_research_round": 0.35,
        # [추가] 일반 진단의 동기 모델 호출은 스레드로 분리했지만 원격 요청 자체의 취소는 별도다.
        "baseline_timeout_note": "cooperative graph timeout; legacy synchronous HTTP runs in threads and may continue after cancellation until its request timeout",
        "baseline_report_replaced": args.report_mode == "none", "langsmith_tracing": False,
        # [추가] 과거의 자유 서술 보고 평가와 새 사실 출력·문단 참조 요약을 구분한다.
        "workflow_report_contract": "deterministic facts + referenced runbook paragraph summaries; no report model call when no runbook was read",
        "fake_scope": "fixed compiler fixtures only; no oracle responses; not a natural-language benchmark" if args.backend == "fake" else None,
        "ordering_note": "calls use start order; baseline may run monitoring concurrently; multiset match is also reported",
        "semantic_note": "exact supported AST semantics ignoring display name and node IDs; no baseline AST metric; not a report factuality judge",
        "latency_note": "single sequential run without warm-up or statistical confidence; includes original baseline delay; no speedup claim",
    }


# [추가] 현재 설정 모델을 그대로 사용하며 평가 래퍼는 기존 모델의 응답을 보정하지 않는다.
def make_models(backend, calls, context=None):
    if backend == "openai":
        from agent.openai_llm import OpenAIFastLLM, OpenAIQualityLLM
        fast = OpenAIFastLLM(os.environ.get("OPENAI_FAST_MODEL", "gpt-4o-mini"), 0.0)
        quality = OpenAIQualityLLM(os.environ.get("OPENAI_QUALITY_MODEL", "gpt-4o"), 0.0)
    else:
        from agent.fake_llm import FastLLM, QualityLLM
        fast, quality = FastLLM("fast-model-stub", 0.0), QualityLLM("quality-model-stub", 0.0)
    return MeteredModel(fast, backend, calls, context), MeteredModel(quality, backend, calls, context)


# [추가] 최초 후보의 형식·정상 종료와 최종 후보의 의미 일치를 별도로 계산한다.
def semantic_scores(case, compiled, calls):
    first = next((call for call in calls if call["method"] == "compile_workflow"), {})
    checked = validate_workflow(first.get("response_content", ""), load_catalog())
    first_valid = checked.valid and first.get("complete") is True and not first.get("refusal")
    return {
        "first_static_valid": bool(first_valid),
        "first_semantic_exact": bool(first_valid and score_definition(checked.workflow, case["expected_definition"])["exact"]) if case["supported"] else None,
        "final_semantic": score_definition(compiled.workflow, case["expected_definition"]) if case["supported"] and compiled.valid else {"exact": False, "errors": ["실행 가능한 절차 생성 실패"]} if case["supported"] else None,
        "unsupported_correct": (not compiled.valid and compiled.error.code == "UNSUPPORTED_REQUEST") if not case["supported"] else None,
    }


# [추가] 분모와 실패를 유지해 생성에 성공한 문항만 골라 성공률을 계산하지 않는다.
def summarize(rows, scenarios, all_calls):
    supported = [row for row in rows if row["supported"]]
    unsupported = [row for row in rows if not row["supported"]]
    summary = {
        "cases_attempted": len(rows), "supported_attempted": len(supported), "unsupported_attempted": len(unsupported),
        "first_semantic_correct": sum(row["scores"]["first_semantic_exact"] for row in supported),
        "final_semantic_correct": sum(row["scores"]["final_semantic"]["exact"] for row in supported),
        "unsupported_explicitly_rejected": sum(row["scores"]["unsupported_correct"] for row in unsupported),
        "all_model_usage": usage_total(all_calls),
        "generation_usage": usage_total([call for call in all_calls if call.get("phase") == "generation"]),
        "execution_report_usage": {arm: usage_total([call for call in all_calls if call.get("arm") == arm and call.get("phase") in ("original_graph", "execution_report")]) for arm in ("baseline", "workflow")},
        "reuse_probe_usage": usage_total([call for call in all_calls if call.get("phase") == "saved_reuse_probe"]),
        "baseline_routes": dict(Counter(row["baseline"].get("route", "failed") for row in scenarios if "baseline" in row)),
        "paths": {},
    }
    for method in ("baseline", "workflow"):
        selected = [row for row in scenarios if row["supported"] and method in row]
        scores = [row[method]["score"] for row in selected]
        domains = [call for row in selected for call in row[method]["calls"]]
        coverage = [score["target_coverage"] for score in scores if score["target_coverage"] is not None]
        times = [row[method]["elapsed_ms"] for row in selected if row[method]["status"] == "completed"]
        summary["paths"][method] = {
            "scenarios": len(selected), "exact_paths": sum(score["path_match"] for score in scores),
            "call_multiset_matches": sum(score["call_multiset_match"] for score in scores),
            "forbidden_calls": sum(score["forbidden_calls"] for score in scores),
            "monitoring_calls": sum(call["source"] == "monitoring" for call in domains),
            "runbook_calls": sum(call["source"] == "runbook" for call in domains),
            "discovery_calls": sum(event["source"] == "discovery" for row in selected for event in row[method]["mcp_events"]),
            "prompt_calls": sum(event["source"] == "prompt" for row in selected for event in row[method]["mcp_events"]),
            "mean_target_coverage": sum(coverage) / len(coverage) if coverage else None,
            "completed_executions": len(times), "median_completed_execution_ms": median(times) if times else None,
        }
    return summary


# [추가] 실제 호출 전 검토할 문항·카탈로그·프롬프트를 인증 헤더 없이 파일로 제공한다.
def payload_preview(cases):
    first = next(case for case in cases if case["supported"])
    return {
        "destination": "https://api.openai.com/v1/chat/completions",
        "authentication": "OPENAI_API_KEY from local environment; value never included in artifacts",
        "compile_queries": [{"id": case["id"], "query": case["query"]} for case in cases],
        "catalog": load_catalog().to_dict(),
        "sample_compile_messages": build_workflow_messages(first["query"], load_catalog().to_dict(), workflow_response_schema()),
        "report_payload_categories": {
            "baseline": ["사용자 평가 문항", "고정 모의 모니터링 값", "실제로 읽은 data/runbooks/*.md 본문", "이전 보고 검증 피드백"],
            "workflow": ["실제로 읽은 런북의 문단·자료 ID", "이전 요약의 구조·자료 참조 검증 피드백"],
        },
        "source_files": ["eval/cases/workflow_cases.json", "eval/fixtures/monitoring.json", "data/runbooks/*.md", "prompts/workflow_compile.txt", "prompts/workflow_report.txt"],
        "excluded": [".env 파일 원문", "API 키의 출력·결과 저장", "기존 session.db·workflow.db", "사용자의 기존 대화·저장 절차"],
    }


# [추가] 매 문항 후 결과를 저장해 오류·중단 시 이미 측정한 기록을 잃지 않는다.
def write_artifacts(output: Path, data):
    output.mkdir(parents=True, exist_ok=True)
    (output / "results.json").write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    summary = data["summary"]
    # [추가] 실행하지 않은 baseline의 숫자 0은 원자료에 보존하고 Markdown에서만 미측정으로 구분한다.
    baseline_omitted = data["manifest"]["mode"] == "workflow"
    lines = ["<!-- [추가] 평가 CLI가 실제 측정 기록에서 생성한 보고서다. -->", "# 워크플로 비교 평가", "",
             f"- 상태: {data['status']}", f"- 백엔드: {data['manifest']['backend']}",
             f"- 시도/선택 문항: {summary['cases_attempted']}/{len(data['manifest']['selected_case_ids'])}",
             f"- 실행 모드: {data['manifest']['mode']}",
             f"- 보고 모드: {data['manifest']['report_mode']}", ""]
    if data["manifest"]["backend"] == "fake":
        lines += ["**fake는 고정 응답·실행 계측 확인용이다. 아래 수치를 실제 자연어 정확도나 OpenAI 성능으로 해석하지 않는다.**", ""]
        # [추가] workflow 전용 실행에서 비어 있는 baseline 라우팅을 실행 결과처럼 설명하지 않는다.
        if baseline_omitted:
            lines += ["fake의 등록된 정상 생성 응답만 사용했다. 이 회차는 workflow만 실행했으며 baseline은 미측정이다.", ""]
        else:
            lines += [f"fake의 등록된 정상 생성 응답만 사용했다. baseline 라우팅 결과는 {summary['baseline_routes']}다. 채팅으로 분류된 요청의 조회 0회는 조건 지시 준수나 안전한 거절을 입증하지 않는다.", ""]
    lines += [f"지원 문항의 최초/최종 의미 일치: {summary['first_semantic_correct']}/{summary['supported_attempted']}, {summary['final_semantic_correct']}/{summary['supported_attempted']}."]
    # [추가] 선택한 미지원 문항이 없을 때는 0/0을 거절 점수로 표시하지 않는다.
    if summary["unsupported_attempted"]:
        lines += [f"미지원 요청의 명시적 거절: {summary['unsupported_explicitly_rejected']}/{summary['unsupported_attempted']}. 통신·생성 오류는 정답 거절에 포함하지 않는다."]
    else:
        lines += ["미지원 요청의 명시적 거절: 미측정(선택한 미지원 문항 0개). 거절률은 계산하지 않는다."]
    lines += ["", "| 방식 | 지원 시나리오 | 경로 일치 | 호출 집합 일치 | 추가·중복 호출 | 모니터링 / 런북 |", "|---|---:|---:|---:|---:|---:|"]
    for method, values in summary["paths"].items():
        if method == "baseline" and baseline_omitted:
            lines.append("| baseline(미측정) | — | — | — | — | — |")
        else:
            lines.append(f"| {method} | {values['scenarios']} | {values['exact_paths']} | {values['call_multiset_matches']} | {values['forbidden_calls']} | {values['monitoring_calls']} / {values['runbook_calls']} |")
    guard = data["guard_checks"]
    lines += ["", f"무효 AST의 MCP 실행 전 차단: {guard['blocked_before_mcp']}/{guard['total']}."]
    lines += ["", "| 방식 | 탐색 / 프롬프트 호출 | 완료 실행 수 | 완료 실행 지연 중앙값(ms) |", "|---|---:|---:|---:|"]
    for method, values in summary["paths"].items():
        if method == "baseline" and baseline_omitted:
            lines.append("| baseline(미측정) | — | — | — |")
        else:
            elapsed = values["median_completed_execution_ms"]
            lines.append(f"| {method} | {values['discovery_calls']} / {values['prompt_calls']} | {values['completed_executions']} | {round(elapsed, 2) if elapsed is not None else '미측정'} |")
    lines += ["", "지연 표는 완료된 실행만의 중앙값이며 생성 시간은 별도다. 각 방식의 완료 수와 원본 고정 지연을 함께 확인해야 한다."]
    lines += ["", "## 실패·불일치 문항", ""]
    failures = [row for row in data["cases"] if (row["supported"] and not row["scores"]["final_semantic"]["exact"]) or (not row["supported"] and not row["scores"]["unsupported_correct"])]
    # [추가] AST 의미 점수가 없는 미지원 문항은 명시적 거절 실패로 설명하고 원래 오류를 함께 남긴다.
    for row in failures:
        error = row["compilation"].get("error")
        if row["supported"]:
            reason = error or "의미 불일치"
        elif row["compilation"]["valid"]:
            reason = "미지원 요청을 실행 가능한 절차로 생성함(명시적 거절 실패)"
        else:
            reason = f"미지원 요청을 명시적으로 거절하지 못함: {error}"
        lines.append(f"- {row['id']}: {reason}")
    if not failures:
        lines.append("- 없음")
    # [추가] 표시상의 미측정 표기는 JSON의 집계·점수·분모를 바꾸지 않음을 보고서에 밝힌다.
    baseline_note = ("- `--mode workflow`이므로 이 회차에는 baseline을 실행하지 않았다. baseline의 미측정 표시는 원자료의 숫자 집계를 변경하지 않으며 0점·조회 억제 성과를 뜻하지 않는다."
                     if baseline_omitted else "- baseline 코드의 각 연구 작업에는 0.35초 고정 지연이 남아 있다. 같은 라운드의 병렬 작업에서 합산하지 않으며 chat 직행 시 이 단계는 실행하지 않는다. 지연 수치로 서비스 속도 향상을 주장하지 않는다.")
    lines += ["", "## 해석 범위", "", baseline_note,
              "- 병렬 baseline은 호출 시작 순서가 달라질 수 있어 호출 집합 일치도 함께 제시한다.",
              "- baseline에는 워크플로 AST·명시적 지원 거절 계약이 없어 해당 자연어 구조 지표를 만들어내지 않는다.",
              "- 미측정 표시는 Markdown 표현만 구분한다. results.json의 숫자·분모·채점 결과와 구조는 그대로 기록한다.",
              "- 생성 실패도 지원 시나리오의 분모에 포함한다. 사용량 누락은 null이며 실제 청구액으로 환산하지 않는다.",
              "- none 보고 모드는 경로 비교 전용이다. full 모드의 지연·보고·토큰과 혼합하지 않는다.",
              "- JSON의 cases/scenarios에 후보·trace·조회·모델 호출·지연·사용량·저장 재실행 기록을 남겼다.",
              "- dev/holdout은 데이터셋의 원래 구분이다. 이미 확인한 문항의 재측정은 독립된 미관측 평가가 아니다.",
              f"- 이번 측정 역할: {data['manifest'].get('evaluation_role', 'initial')}. regression은 기존 실패를 확인하고 보완한 뒤 같은 문항을 다시 측정한 결과다."]
    if data.get("blocked_reason"):
        lines += ["", "실제 평가 중단 사유: " + json.dumps(data["blocked_reason"], ensure_ascii=False)]
    (output / "report.md").write_text("\n".join(lines) + "\n")


# [추가] 각 시나리오에서 두 방식에 같은 스냅샷을 주고, 저장된 계획은 다시 생성하지 않는다.
async def evaluate(args, cases, output):
    calls = []
    data = {"manifest": manifest(args, cases), "status": "running", "cases": [], "scenarios": [], "model_calls": calls, "blocked_reason": None}
    data["guard_checks"] = await run_guard_checks()
    with tempfile.TemporaryDirectory(prefix="workflow-evaluation-") as directory:
        store = SqliteWorkflowStore(Path(directory) / "workflow.db")
        try:
            for case in cases:
                context = {"case_id": case["id"], "arm": "workflow", "phase": "generation", "scenario_id": None}
                fast, quality = make_models(args.backend, calls, context)
                start_index, started = len(calls), perf_counter()
                compiled, record = await generate_saved(case["query"], fast, store, args.timeout)
                generation_ms = (perf_counter() - started) * 1000
                generation_calls = deepcopy_json(calls[start_index:])
                row = {"id": case["id"], "split": case["split"], "query": case["query"], "supported": case["supported"],
                       "compilation": compiled.model_dump(mode="json"), "generation_ms": generation_ms,
                       "saved_workflow_id": record.workflow_id if record else None,
                       "saved_definition_hash": record.definition_hash if record else None,
                       "model_calls": generation_calls, "generation_usage": usage_total(generation_calls),
                       "scores": semantic_scores(case, compiled, generation_calls)}
                data["cases"].append(row)
                fatal = fatal_provider_error(calls[start_index:])
                if fatal:
                    data["status"], data["blocked_reason"] = "blocked", fatal
                    break
                for index, scenario in enumerate(case["scenarios"]):
                    context.update(arm="workflow", phase="execution_report", scenario_id=scenario["id"])
                    measured = {"case_id": case["id"], "scenario_id": scenario["id"], "supported": case["supported"], "expected_calls": scenario["expected_calls"]}
                    begin = len(calls)
                    if record:
                        # [추가] DB에서 새 사본을 읽어 실제 저장 계약을 거친 정의만 실행한다.
                        saved = await asyncio.to_thread(store.get, record.workflow_id)
                        candidate = await run_saved(saved, case["query"], scenario["snapshots"], quality, args.report_mode, args.timeout)
                    else:
                        candidate = {"status": "not_executed", "calls": [], "mcp_events": [], "elapsed_ms": None}
                    candidate.update(score=score_calls(candidate["calls"], scenario["expected_calls"]),
                                     usage=usage_total(calls[begin:]), execution_kind="initial" if index == 0 else "saved_reuse")
                    candidate["definition_hash"] = record.definition_hash if record else None
                    if index == 0:
                        candidate["generation_plus_execution_ms"] = generation_ms + candidate["elapsed_ms"] if candidate["elapsed_ms"] is not None else None
                    measured["workflow"] = candidate
                    fatal = fatal_provider_error(calls[begin:])
                    if not fatal and args.mode == "compare":
                        context.update(arm="baseline", phase="original_graph")
                        begin = len(calls)
                        baseline = await run_baseline(case["query"], scenario["snapshots"], fast, quality, args.report_mode, args.timeout)
                        baseline.update(score=score_calls(baseline["calls"], scenario["expected_calls"]), usage=usage_total(calls[begin:]))
                        measured["baseline"] = baseline
                        fatal = fatal_provider_error(calls[begin:])
                    if not fatal and record and index == 0 and args.reuse_first:
                        context.update(arm="workflow", phase="saved_reuse_probe")
                        begin = len(calls)
                        reuse = await run_saved(await asyncio.to_thread(store.get, record.workflow_id), case["query"], scenario["snapshots"], quality, args.report_mode, args.timeout)
                        reuse.update(usage=usage_total(calls[begin:]), compile_calls=sum(item["method"] == "compile_workflow" for item in calls[begin:]))
                        measured["reuse_probe"] = reuse
                        fatal = fatal_provider_error(calls[begin:])
                    data["scenarios"].append(measured)
                    if fatal:
                        data["status"], data["blocked_reason"] = "blocked", fatal
                        break
                data["summary"] = summarize(data["cases"], data["scenarios"], calls)
                write_artifacts(output, data)
                print(f"{case['id']}: generated={compiled.valid}, scenarios={len(case['scenarios'])}", flush=True)
                if fatal:
                    break
        finally:
            store.close()
    if data["status"] == "running":
        data["status"] = "completed"
        # [추가] 전 문항이 통신·모델 오류로 끝났다면 측정 성공으로 반환하지 않는다.
        if not any(row["compilation"]["valid"] or row["scores"]["unsupported_correct"] for row in data["cases"]):
            data["status"] = "failed"
    data["summary"] = summarize(data["cases"], data["scenarios"], calls)
    write_artifacts(output, data)
    return data


# [추가] JSON 직렬화 가능한 측정 기록만 복사해 다음 시나리오의 갱신과 분리한다.
def deepcopy_json(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


# [추가] 외부 호출 전 미리보기와 offline/live 실행을 명시적 CLI 옵션으로 분리한다.
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("fake", "openai"), default="fake")
    parser.add_argument("--split", choices=("dev", "holdout", "all"), default="dev")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--mode", choices=("compare", "workflow"), default="compare")
    parser.add_argument("--report-mode", choices=("full", "none"), default="full")
    # [추가] 원래 데이터 분류를 유지하면서 사후 회귀 평가임을 결과에 함께 기록한다.
    parser.add_argument("--evaluation-role", choices=("initial", "regression"), default="initial")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--reuse-first", action="store_true")
    parser.add_argument("--preview-only", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    if (args.limit is not None and args.limit < 1) or not 0 < args.timeout <= 600:
        parser.error("limit은 1 이상, timeout은 0 초과 600 이하여야 합니다.")
    # [추가] 로컬 .env는 설정에만 사용하고 LangSmith로 평가 데이터를 자동 전송하지 않는다.
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    load_dotenv(ROOT / ".env", override=False)
    cases = load_cases(split=args.split, limit=args.limit)
    output = args.output_dir or ROOT / "eval/results" / (args.backend + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    if (output / "results.json").exists():
        parser.error("기존 측정 결과를 덮어쓰지 않도록 새 output-dir를 지정하세요.")
    output.mkdir(parents=True, exist_ok=True)
    (output / "payload_preview.json").write_text(json.dumps(payload_preview(cases), ensure_ascii=False, indent=2) + "\n")
    if args.preview_only:
        print("외부 호출 없이 전송 미리보기를 작성했습니다: " + str(output / "payload_preview.json"))
        return 0
    if args.backend == "openai" and not os.environ.get("OPENAI_API_KEY", "").strip():
        print("OPENAI_API_KEY가 없어 실제 평가는 시작하지 않았습니다.", file=sys.stderr)
        return 2
    data = asyncio.run(evaluate(args, cases, output))
    print(json.dumps({"status": data["status"], "summary": data["summary"], "output": str(output)}, ensure_ascii=False))
    return 2 if data["status"] == "blocked" else 1 if data["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
