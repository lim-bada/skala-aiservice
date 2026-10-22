# [추가] 새 파일 전체: 일반 진단의 짧은 동기 모델 요청이 무제한 추론으로 바뀌지 않게 검증한다.
"""로컬 모델 실행 없이 HTTP 요청과 기존 응답 처리를 함께 검사한다."""
import json
import unittest
from unittest.mock import patch

import httpx

from agent.ollama_llm import OLLAMA_URL, OllamaFastLLM


# [추가] route·plan·analyze가 같은 제한을 적용하고 기존 반환 계약을 유지하는지 확인한다.
class OllamaLegacyRequestTests(unittest.TestCase):
    def test_short_model_phases_disable_thinking_and_bound_output(self):
        model = OllamaFastLLM("qwen3:0.6b", 0.0)
        requests = []

        # [추가] 공급자 응답·사용량을 모의해 실제 네트워크를 호출하지 않는다.
        def respond(url, *, json: dict, timeout):
            requests.append((url, json, timeout))
            content = '{"route":"incident","needs_analysis":false,"tasks":[]}'
            return httpx.Response(200, request=httpx.Request("POST", url), json={
                "message": {"content": content}, "prompt_eval_count": 8, "eval_count": 5,
            })

        with patch("agent.ollama_llm.httpx.post", side_effect=respond):
            route, usage = model.route("web-01 점검")
            self.assertEqual(route["route"], "incident")
            self.assertEqual(usage["tokens"], 13)
            plan, _ = model.plan("web-01 점검", "모니터링 조회")
            self.assertEqual(plan["tasks"][0]["target"], "web-01")
            text, _ = model.analyze([])
            self.assertIsInstance(json.loads(text), dict)
        self.assertEqual(len(requests), 3)
        for url, payload, timeout in requests:
            with self.subTest(messages=payload["messages"]):
                self.assertEqual(url, OLLAMA_URL)
                self.assertIs(payload["think"], False)
                self.assertFalse(payload["stream"])
                self.assertEqual(payload["options"], {"temperature": 0.0, "num_predict": 1024})
                self.assertEqual(timeout, 300.0)


if __name__ == "__main__":
    unittest.main()
