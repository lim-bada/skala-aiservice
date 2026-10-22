# [추가] 새 파일 전체: baseline 요청은 유지하며 실측 사용량의 누락·0·부분 보고를 구분한다.
import unittest
from unittest.mock import patch

import httpx

from agent.openai_llm import OPENAI_URL, OpenAIFastLLM


# [추가] 실제 키·네트워크 대신 고정 응답으로 기존 _chat 호환성과 새 계측 필드를 검사한다.
class OpenAILegacyUsageTests(unittest.TestCase):
    def test_actual_counts_and_request_contract_are_preserved(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "unit-test-key"}):
            model = OpenAIFastLLM("gpt-4o-mini", .1)
        response = httpx.Response(200, request=httpx.Request("POST", OPENAI_URL), json={
            "choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        })
        with patch("agent.openai_llm.httpx.post", return_value=response) as post:
            content, usage = model._chat("JSON system", "user", json_mode=True)
        self.assertEqual(content, "{}")
        self.assertEqual((usage["tokens"], usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]), (10, 7, 3, 10))
        self.assertEqual(usage["measurement"], "reported")
        self.assertEqual(post.call_args.kwargs["json"], {"model": "gpt-4o-mini", "messages": [{"role": "system", "content": "JSON system"}, {"role": "user", "content": "user"}], "temperature": 0, "response_format": {"type": "json_object"}})

    def test_missing_partial_and_zero_usage_remain_distinct(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": "unit-test-key"}):
            model = OpenAIFastLLM("gpt-4o-mini", .1)
        for reported, expected in [({}, (None, None, None)), ({"prompt_tokens": 2}, (2, None, None)), ({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, (0, 0, 0))]:
            with self.subTest(usage=reported):
                response = httpx.Response(200, request=httpx.Request("POST", OPENAI_URL), json={"choices": [{"message": {"content": "hello"}}], "usage": reported})
                with patch("agent.openai_llm.httpx.post", return_value=response):
                    _, usage = model._chat("system", "user")
                self.assertEqual((usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]), expected)


if __name__ == "__main__":
    unittest.main()
