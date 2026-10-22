# [추가] 보관한 평가 토큰을 공식 단가로 환산하는 오프라인 비용 계산기 전체.
"""표준 입력·출력 단가를 적용한 추정치다. API·.env·사용자 DB에 접근하지 않는다."""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
# [추가] 2026-10-22 공식 모델 문서에서 확인한 일반 텍스트 요청의 100만 토큰당 USD다.
PRICES = {
    "openai:gpt-4o-mini": {"input": Decimal("0.15"), "output": Decimal("0.60")},
    "openai:gpt-4o": {"input": Decimal("2.50"), "output": Decimal("10.00")},
}
RUN_NAMES = (
    "openai-dev", "openai-holdout", "openai-smoke-run",
    "openai-v2-regression", "openai-v3-regression",
)


# [추가] 누락·추정 토큰이나 알 수 없는 모델을 비용 0으로 처리하지 않고 계산을 중단한다.
def price_call(call: dict) -> dict:
    usage = call.get("usage", {})
    model = call.get("model")
    if call.get("error") or usage.get("measurement") != "reported":
        raise ValueError("오류 호출 또는 공급자 실측이 아닌 사용량은 계산할 수 없습니다.")
    if model not in PRICES:
        raise ValueError("공식 단가를 등록하지 않은 모델이 있습니다.")
    counts = [usage.get(key) for key in ("input_tokens", "output_tokens", "total_tokens")]
    if any(type(value) is not int or value < 0 for value in counts):
        raise ValueError("사용량에는 음수가 아닌 정수 입력·출력·총 토큰이 모두 필요합니다.")
    incoming, outgoing, combined = counts
    if incoming + outgoing != combined:
        raise ValueError("입력·출력 토큰의 합과 총 토큰이 다릅니다.")
    if usage.get("model", model) != model:
        raise ValueError("호출 모델과 사용량 모델이 다릅니다.")
    rates = PRICES[model]
    amount = (incoming * rates["input"] + outgoing * rates["output"]) / 1_000_000
    return {
        "model": model, "input_tokens": incoming, "output_tokens": outgoing,
        "total_tokens": combined, "estimated_cost_usd": str(amount),
    }


# [추가] 호출별 금액을 반올림하지 않고 합산하며 USD는 소수 문자열로 보존한다.
def summarize(rows: list[dict]) -> dict:
    return {
        "model_calls": len(rows),
        **{key: sum(row[key] for row in rows)
           for key in ("input_tokens", "output_tokens", "total_tokens")},
        "estimated_cost_usd": str(sum(
            (Decimal(row["estimated_cost_usd"]) for row in rows), Decimal(0),
        )),
    }


# [추가] 중첩 복사본 대신 최상위 model_calls만 집계하고 파일별 원래 합계와 대조한다.
def analyze_costs() -> dict:
    all_rows, runs = [], []
    for run_name in RUN_NAMES:
        path = ROOT / "eval" / "results" / run_name / "results.json"
        raw = path.read_bytes()
        data = json.loads(raw)
        if data.get("status") != "completed" or data["manifest"]["backend"] != "openai":
            raise ValueError(f"완료된 OpenAI 평가 기록이 아닙니다: {run_name}")
        cases = {case["id"]: case for case in data["cases"]}
        rows = []
        for call in data["model_calls"]:
            case = cases[call["case_id"]]
            if type(case["supported"]) is not bool:
                raise ValueError("문항의 지원 여부가 올바르지 않습니다.")
            rows.append({
                **price_call(call), "run": run_name, "case_id": call["case_id"],
                "scenario_id": call.get("scenario_id"), "split": case["split"],
                "supported": case["supported"], "arm": call["arm"],
                "phase": call["phase"], "method": call["method"],
            })
        totals = summarize(rows)
        recorded = data["summary"]["all_model_usage"]
        for key in ("model_calls", "input_tokens", "output_tokens", "total_tokens"):
            if recorded[key] != totals[key]:
                raise ValueError(f"평가 기록의 합계와 호출별 집계가 다릅니다: {run_name}, {key}")
        runs.append({
            "run": run_name, "source": str(path.relative_to(ROOT)),
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "measurement_timestamp": data["manifest"]["timestamp"],
            "supported_cases": sum(case["supported"] for case in data["cases"]),
            "supported_scenarios": sum(row["supported"] for row in data["scenarios"]),
            **totals,
        })
        all_rows.extend(rows)

    # [추가] 전후 비교는 초기 dev·holdout의 같은 지원 문항·시나리오로 범위를 맞춘다.
    initial = [row for row in all_rows if row["run"] in RUN_NAMES[:2] and row["supported"]]
    groups = defaultdict(list)
    for row in all_rows:
        groups[(row["run"], row["supported"], row["arm"], row["phase"], row["model"])].append(row)
    supported_runs = runs[:2]
    comparison = {
        "supported_cases": sum(run["supported_cases"] for run in supported_runs),
        "supported_scenarios": sum(run["supported_scenarios"] for run in supported_runs),
        "baseline": summarize([row for row in initial if row["arm"] == "baseline"]),
        "workflow": summarize([row for row in initial if row["arm"] == "workflow"]),
        "workflow_generation": summarize([row for row in initial if row["phase"] == "generation"]),
        "workflow_execution_report": summarize([row for row in initial if row["phase"] == "execution_report"]),
    }
    before = Decimal(comparison["baseline"]["estimated_cost_usd"])
    after = Decimal(comparison["workflow"]["estimated_cost_usd"])
    if before:
        comparison["workflow_to_baseline_ratio"] = format(after / before, ".4f")
        comparison["increase_percent"] = format((after / before - 1) * 100, ".2f")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pricing_checked_on": "2026-10-22",
        "pricing_sources": [
            "https://developers.openai.com/api/docs/models/gpt-4o-mini",
            "https://developers.openai.com/api/docs/models/gpt-4o",
        ],
        "prices_usd_per_million_tokens": {
            model: {key: str(value) for key, value in rates.items()}
            for model, rates in PRICES.items()
        },
        "cost_basis": "recorded provider tokens multiplied by standard uncached text rates; not an invoice",
        "limitations": [
            "Cache-detail tokens are not retained in these source records; no cache discount applied.",
            "Taxes, currency conversion, credits, tracing and infrastructure charges are excluded.",
            "These are historical evaluation files, not current-version or all interactive usage.",
            "Generation is counted once per question, including recorded repair attempts; scenarios reuse definitions.",
            "Different failed, skipped and short-circuit paths remain included; not equal-quality successful-task costs.",
        ],
        "initial_supported_comparison": comparison,
        "runs": runs, "all_archived_runs": summarize(all_rows),
        "groups": [
            {"run": key[0], "supported": key[1], "arm": key[2], "phase": key[3],
             "model": key[4], **summarize(rows)}
            for key, rows in sorted(groups.items())
        ],
    }


# [추가] 기존 평가 원본은 유지하고 계산 결과만 별도의 로컬 산출물로 저장한다.
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path,
        default=ROOT / "eval/results/openai-cost-analysis/summary.json",
    )
    args = parser.parse_args()
    try:
        result = analyze_costs()
    except (OSError, ValueError, KeyError) as error:
        parser.error(f"보관된 평가 원자료를 확인하세요: {error}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "initial_supported_comparison": result["initial_supported_comparison"],
        "all_archived_runs": result["all_archived_runs"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
