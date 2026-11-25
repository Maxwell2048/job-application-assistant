"""LangChain retriever and chat adapter.

The retriever must rank exactly like the classic one; the adapter must send
the same request settings, keep the program-side validation and retries, and
map service errors to the same error types. A local HTTP server stands in for
the model service; no real model or network is used."""
import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import local_llm  # noqa: E402
from local_llm import ConfigError, LLMError, RequestRejectedError, ServiceError, build_llm, resolve_local_model  # noqa: E402
from search_experience import ExperienceRetriever, make_retriever  # noqa: E402

try:
    import langchain_client
    from langchain_retrieval import LangChainExperienceRetriever, SectionRetriever
except ImportError:  # pragma: no cover - LangChain is in requirements-test.txt
    langchain_client = None


class CharacterTokenizer:
    def __call__(self, text, **kwargs):
        return {"offset_mapping": [(i, i + 1) for i in range(len(text))]}

    def encode(self, text):
        return list(text)


class BagOfCharactersModel:
    """Normalised character-count vectors, so dot product and cosine agree."""
    max_seq_length = 100000

    def __init__(self):
        self.tokenizer = CharacterTokenizer()

    def encode(self, texts, **kwargs):
        texts = [texts] if isinstance(texts, str) else texts
        vectors = np.zeros((len(texts), 97))
        for row, text in enumerate(texts):
            for char in text:
                vectors[row, ord(char) % 97] += 1
        return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


def make_corpus(folder):
    (folder / "proj_a.md").write_text(
        "# 项目 A\n\n## 我的贡献 图片标注\n我完成了 12 幅示例地图的图片标注，并参与目标检测训练。\n"
        "相关能力：Image Annotation\n\n## 我的工作 部署\n用 Docker Compose 部署四个服务并做部署测试。\n", encoding="utf-8")
    (folder / "proj_b.md").write_text(
        "# 项目 B\n\n## 我的工作 参数搜索\n我使用 GridSearchCV 对 SVM 的 C 与 gamma 执行参数搜索。\n"
        "相关能力：Hyperparameter Search\n\n## 我的工作 报告\n用 Power BI 和 SQL 做销售分析看板。\n", encoding="utf-8")


@unittest.skipIf(langchain_client is None, "LangChain is not installed")
class LangChainRetrieverTests(unittest.TestCase):
    def build(self, cls, folder):
        retriever = cls(folder)
        retriever._load_model = lambda: BagOfCharactersModel()
        with contextlib.redirect_stderr(io.StringIO()):
            return retriever.build()

    def test_ranking_matches_the_classic_retriever(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            make_corpus(folder)
            classic = self.build(ExperienceRetriever, folder)
            chain = self.build(LangChainExperienceRetriever, folder)
            for query in ("图片标注 object detection", "SQL dashboard", "Docker 部署", "hyperparameter search"):
                with self.subTest(query=query):
                    expected, actual = classic.search(query, top_k=3), chain.search(query, top_k=3)
                    self.assertEqual([r["section_id"] for r in actual], [r["section_id"] for r in expected])
                    for a, b in zip(actual, expected):
                        self.assertAlmostEqual(a["score"], b["score"], places=3)
                        self.assertEqual(a["text"], b["text"])

    def test_langchain_retriever_returns_section_documents_with_citation_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            make_corpus(folder)
            documents = self.build(LangChainExperienceRetriever, folder).as_retriever(top_k=2).invoke("Power BI SQL")
            self.assertEqual(len(documents), 2)
            self.assertIsInstance(documents[0].metadata["line_start"], int)
            self.assertIn("proj_", documents[0].metadata["source"])
            self.assertEqual({d.metadata["section_id"] for d in documents}.__len__(), 2)

    def test_factory_and_config_validation(self):
        self.assertIsInstance(make_retriever("native", "x"), ExperienceRetriever)
        self.assertIsInstance(make_retriever("langchain", "x"), LangChainExperienceRetriever)
        with self.assertRaises(ValueError):
            make_retriever("faiss", "x")


class FakeModelService:
    """OpenAI-compatible endpoint on a free loopback port: answers queued
    (status, body) pairs and records each request body."""

    def __init__(self, answers):
        self.answers, self.requests = list(answers), []
        service = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                service.requests.append(json.loads(body))
                status, answer = service.answers.pop(0)
                data = json.dumps(answer).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def completion(content, finish="stop"):
    return 200, {"id": "x", "object": "chat.completion", "created": 0, "model": "m",
                 "choices": [{"index": 0, "finish_reason": finish,
                              "message": {"role": "assistant", "content": content}}]}


@unittest.skipIf(langchain_client is None, "LangChain is not installed")
class LangChainAdapterTests(unittest.TestCase):
    SCHEMA = {"type": "object", "additionalProperties": False, "required": ["answer"],
              "properties": {"answer": {"type": "string"}}}

    def adapter(self, service, **changes):
        config = {"adapter": "langchain_openai", "model": "m", "base_url": service.base_url, "api_key": "secret-key",
                  "context_budget": 8192, "max_output_tokens": 256, "timeout_seconds": 10,
                  "response_format": "json_object", "extra_body": {"chat_template_kwargs": {"enable_thinking": False},
                                                                   "top_k": 1}, **changes}
        llm = build_llm(config)
        self.assertIsInstance(llm, langchain_client.LangChainOpenAIAdapter)
        return llm

    def serve(self, *answers):
        service = FakeModelService(answers)
        self.addCleanup(service.close)
        return service

    def generate(self, llm, **kwargs):
        with contextlib.redirect_stderr(io.StringIO()):
            return llm.generate_json("t", "system", {"q": 1}, self.SCHEMA, **kwargs)

    def test_request_carries_the_same_settings_as_the_classic_adapter(self):
        service = self.serve(completion('{"answer": "ok"}'))
        self.assertEqual(self.generate(self.adapter(service)), {"answer": "ok"})
        body = service.requests[0]
        self.assertEqual(body["model"], "m")
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["max_tokens"], 256)                  # read by servers that ignore the newer field
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(body["top_k"], 1)
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])

    def test_program_validation_and_correction_still_apply(self):
        service = self.serve(completion('{"answer": "bad"}'), completion('{"answer": "good"}'))
        result = self.generate(self.adapter(service),
                               validate=lambda d: [] if d["answer"] == "good" else ["answer 必须是 good"])
        self.assertEqual(result, {"answer": "good"})
        correction = service.requests[1]["messages"]
        self.assertEqual(correction[2]["role"], "assistant")
        self.assertIn("answer 必须是 good", correction[3]["content"])

    def test_truncated_and_rejected_answers(self):
        service = self.serve(*[completion('{"answer": "o', finish="length")] * 3)
        with self.assertRaisesRegex(LLMError, "finish_reason=length"):
            self.generate(self.adapter(service))
        service = self.serve((400, {"error": {"message": "bad field secret-key", "type": "invalid_request_error"}}))
        with self.assertRaises(RequestRejectedError) as error:
            self.generate(self.adapter(service))
        self.assertNotIn("secret-key", str(error.exception))
        self.assertEqual(len(service.requests), 1)                # a rejected request is not retried

    def test_structured_output_failure_is_a_structure_error(self):
        service = self.serve((500, {"error": {"code": "structured_output_failed", "message": "no json"}}),
                             completion('{"answer": "ok"}'))
        self.assertEqual(self.generate(self.adapter(service)), {"answer": "ok"})

    def test_unreachable_service_is_a_service_error(self):
        llm = self.adapter(FakeModelService([]))
        llm.base_url = "http://127.0.0.1:9/v1"
        llm.max_attempts = 1
        with self.assertRaises(LLMError) as error:
            self.generate(llm)
        self.assertEqual(error.exception.error_type, ServiceError("x").error_type)

    def test_tracing_must_be_off(self):
        for name in langchain_client.TRACING_VARIABLES:
            with self.subTest(name=name):
                with self.assertRaises(ConfigError):
                    langchain_client.assert_tracing_off({name: "true"})
        langchain_client.assert_tracing_off({"LANGSMITH_TRACING": "false"})

    def test_endpoint_routing_accepts_the_langchain_adapter(self):
        config = {"adapter": "langchain_openai", "model": "m", "local_model_endpoints": [
            {"base_url": "http://127.0.0.1:18081/v1", "model": "m"}]}
        response = io.BytesIO(json.dumps({"data": [{"id": "m"}]}).encode())
        with mock.patch("local_llm.urllib.request.urlopen", return_value=response):
            self.assertEqual(resolve_local_model(config)["base_url"], "http://127.0.0.1:18081/v1")

    def test_describe_parameters_names_the_client(self):
        params = self.adapter(self.serve()).describe_parameters()
        self.assertEqual(params["adapter"], "langchain_openai")
        self.assertTrue(params["client"].startswith("langchain-openai"))


class ConfigTests(unittest.TestCase):
    def test_unknown_retriever_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text(json.dumps({"adapter": "openai_http", "model": "m", "base_url": "http://127.0.0.1:9/v1",
                                        "context_budget": 8192, "max_output_tokens": 256, "timeout_seconds": 10,
                                        "retriever": "faiss"}), encoding="utf-8")
            with self.assertRaises(ConfigError):
                local_llm.load_config(path)


if __name__ == "__main__":
    unittest.main()
