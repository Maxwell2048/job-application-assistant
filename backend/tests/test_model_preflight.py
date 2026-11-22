import json
import unittest
from unittest.mock import patch
from backend.app.core.config import Settings
from backend.app.services.pipeline import Pipeline
from local_llm import OpenAIHttpAdapter


class ModelPreflightTests(unittest.TestCase):
    def adapter(self, response):
        config={"adapter":"openai_http","model":"fake-model","base_url":"http://127.0.0.1:9/v1",
                "context_budget":50000,"timeout_seconds":5,"max_output_tokens":4096}
        adapter=OpenAIHttpAdapter(config)
        calls=[]
        def answer(*args):calls.append(args);return response
        adapter._call_service=answer
        return config,adapter,calls

    def test_preflight_uses_real_extraction_schema_and_verbatim_quote_validation(self):
        result={"requirements":[{"text":"Python development","category":"technical_skill","importance":"required",
                 "source_quote":"Python development is required.","source_context":"",
                 "importance_source_quote":"required","qualifiers":[],"search_queries":["Python"],"needs_review":False}],"requirement_groups":[]}
        config,adapter,calls=self.adapter(json.dumps(result))
        with patch('backend.app.services.pipeline.load_config',return_value=config),patch('local_llm.build_llm',return_value=adapter):
            valid,detail=Pipeline(Settings(_env_file=None)).model_preflight()
        self.assertTrue(valid,detail);self.assertEqual(len(calls),1)

    def test_preflight_rejects_invalid_output_in_one_attempt_without_creating_runs(self):
        config,adapter,calls=self.adapter('not JSON')
        with patch('backend.app.services.pipeline.load_config',return_value=config),patch('local_llm.build_llm',return_value=adapter):
            valid,detail=Pipeline(Settings(_env_file=None)).model_preflight()
        self.assertFalse(valid);self.assertIn('预检失败',detail);self.assertEqual(len(calls),1)
