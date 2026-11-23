import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from local_llm import OpenAIHttpAdapter,StructureError,LLMError,extract_json_object
from test_local_llm import make_config

SCHEMA={"type":"object","required":["a"],"additionalProperties":False,"properties":{"a":{"type":"integer"}}}

class StructuredOutputTests(unittest.TestCase):
    def test_prose_braces_and_reasoning_do_not_hide_the_actual_result(self):
        self.assertEqual(extract_json_object('<think>Example {"a":999}</think>Here is JSON: ```json\n{"a":1}\n```',SCHEMA),{"a":1})

    def test_one_schema_matching_object_can_be_selected_from_explanation(self):
        self.assertEqual(extract_json_object('Schema: {"type":"object"} Result: {"a":2}',SCHEMA),{"a":2})

    def test_ambiguous_objects_and_truncated_nested_output_are_rejected(self):
        for text in ['{"a":1} and {"a":2}', '{"result":{"a":1},"unfinished":']:
            with self.subTest(text=text), self.assertRaises(StructureError):extract_json_object(text,SCHEMA)

    def test_json_object_request_and_text_content_blocks(self):
        adapter=OpenAIHttpAdapter(make_config(response_format="json_object"))
        reply={"choices":[{"message":{"content":[{"type":"text","text":"{\"a\":"},{"type":"text","text":"3}"}]},"finish_reason":"stop"}]}
        with patch('local_llm.urllib.request.urlopen',return_value=io.BytesIO(json.dumps(reply).encode())) as fetch:
            self.assertEqual(adapter.generate_json('test','sys',{},SCHEMA),{"a":3})
        request=json.loads(fetch.call_args.args[0].data)
        self.assertEqual(request['response_format'],{"type":"json_object"})

    def test_reasoning_only_is_not_used_as_the_answer(self):
        adapter=OpenAIHttpAdapter(make_config())
        reply={"choices":[{"message":{"content":None,"reasoning_content":"{\"a\":1}"},"finish_reason":"stop"}]}
        with patch('local_llm.urllib.request.urlopen',return_value=io.BytesIO(json.dumps(reply).encode())):
            with self.assertRaises(StructureError):adapter._call_service([],5,SCHEMA)

    def test_raw_failures_are_preserved_privately_with_credential_redaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter=OpenAIHttpAdapter(make_config(diagnostics_dir=tmp,api_key='PRIVATE_SECRET'))
            adapter._call_service=lambda *args:'not JSON PRIVATE_SECRET'
            with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(LLMError):adapter.generate_json('extract_requirements','sys',{},SCHEMA)
            files=list(Path(tmp).glob('*.json'));self.assertEqual(len(files),3)
            for path in files:
                saved=path.read_text();self.assertNotIn('PRIVATE_SECRET',saved);self.assertIn('[redacted]',saved)

    def test_correction_does_not_accumulate_malformed_assistant_turns(self):
        adapter=OpenAIHttpAdapter(make_config());messages=[]
        def call(turns,*args):
            messages.append(turns)
            return 'bad' if len(messages)<3 else '{"a":1}'
        adapter._call_service=call
        with contextlib.redirect_stderr(io.StringIO()):self.assertEqual(adapter.generate_json('test','sys',{},SCHEMA),{'a':1})
        self.assertEqual([len(turns) for turns in messages],[2,4,4])

    def test_strata_structured_502_is_a_model_structure_failure(self):
        from urllib.error import HTTPError
        from local_llm import ServiceError
        body=json.dumps({"error":{"code":"structured_output_failed","type":"structured_output_failed","message":"model did not return valid JSON"}}).encode()
        adapter=OpenAIHttpAdapter(make_config())
        error=HTTPError('http://127.0.0.1:9/v1/chat/completions',502,'Bad Gateway',{},io.BytesIO(body))
        with patch('local_llm.urllib.request.urlopen',side_effect=error):
            with self.assertRaises(StructureError) as caught:adapter._call_service([],5,SCHEMA)
        self.assertIn('输出结构错误',str(caught.exception))
        self.assertIn('structured_output_failed',adapter.last_raw_response)
        error=HTTPError('http://127.0.0.1:9/v1/chat/completions',502,'Bad Gateway',{},io.BytesIO(b'{"error":{"message":"upstream unavailable"}}'))
        with patch('local_llm.urllib.request.urlopen',side_effect=error):
            with self.assertRaises(ServiceError):adapter._call_service([],5,SCHEMA)
