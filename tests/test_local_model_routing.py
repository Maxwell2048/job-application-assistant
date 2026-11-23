import io
import json
import unittest
from unittest.mock import patch

from local_llm import ConfigError, ServiceError, resolve_local_model


class LocalModelRoutingTests(unittest.TestCase):
    def config(self):
        return {"adapter": "openai_http", "base_url": "http://127.0.0.1:18081/v1",
                "model": "preferred", "api_key": "private-key", "max_output_tokens": 256,
                "local_model_endpoints": [
                    {"base_url": "http://127.0.0.1:18081/v1", "model": "preferred"},
                    {"base_url": "http://127.0.0.1:18080/v1", "model": "fallback"}]}

    def response(self, name, status="loaded"):
        return io.BytesIO(json.dumps({"data": [{"id": name, "status": {"value": status}}]}).encode())

    def test_prefers_first_loaded_model_and_does_not_mutate_config(self):
        config = self.config()
        with patch("local_llm.urllib.request.urlopen", return_value=self.response("preferred")) as fetch:
            selected = resolve_local_model(config)
        self.assertEqual(selected["model"], "preferred")
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.args[0].full_url, "http://127.0.0.1:18081/v1/models")
        self.assertEqual(selected["max_output_tokens"], 256)
        self.assertEqual(config["model"], "preferred")

    def test_closed_preferred_port_selects_matching_fallback_model(self):
        config = self.config()
        with patch("local_llm.urllib.request.urlopen", side_effect=[OSError("closed"), self.response("fallback")]):
            selected = resolve_local_model(config)
        self.assertEqual(selected["base_url"], "http://127.0.0.1:18080/v1")
        self.assertEqual(selected["model"], "fallback")
        self.assertEqual(config["model"], "preferred")

    def test_wrong_or_loading_model_is_not_considered_available(self):
        for response in (self.response("different"), self.response("preferred", "loading")):
            with self.subTest(response=response), patch("local_llm.urllib.request.urlopen", side_effect=[response, self.response("fallback")]):
                self.assertEqual(resolve_local_model(self.config())["model"], "fallback")

    def test_all_offline_reports_ports_without_credentials(self):
        with patch("local_llm.urllib.request.urlopen", side_effect=OSError("closed")):
            with self.assertRaises(ServiceError) as error:
                resolve_local_model(self.config())
        self.assertIn("18080", str(error.exception))
        self.assertIn("18081", str(error.exception))
        self.assertNotIn("private-key", str(error.exception))

    def test_nonlocal_or_malformed_targets_rejected_before_network(self):
        for url in ("http://example.com:18080/v1", "http://localhost:bad/v1", "http://user:pass@localhost:18080/v1"):
            with self.subTest(url=url):
                config = self.config()
                config["local_model_endpoints"][1]["base_url"] = url
                with patch("local_llm.urllib.request.urlopen") as fetch:
                    with self.assertRaises(ConfigError): resolve_local_model(config)
                    fetch.assert_not_called()

    def test_legacy_config_does_not_probe_network(self):
        config = self.config()
        del config["local_model_endpoints"]
        with patch("local_llm.urllib.request.urlopen") as fetch:
            self.assertIs(resolve_local_model(config), config)
            fetch.assert_not_called()

    def test_malformed_response_falls_back(self):
        with patch("local_llm.urllib.request.urlopen", side_effect=[io.BytesIO(b"not json"), self.response("fallback")]):
            self.assertEqual(resolve_local_model(self.config())["model"], "fallback")


    def test_model_alias_is_resolved_to_the_current_canonical_loaded_model(self):
        payload={"data":[{"id":"actual-q4-model","status":{"value":"loaded"}},
                         {"id":"preferred","alias_of":"actual-q4-model","status":{"value":"loaded"}}]}
        with patch("local_llm.urllib.request.urlopen",return_value=io.BytesIO(json.dumps(payload).encode())):
            selected=resolve_local_model(self.config())
        self.assertEqual(selected['model'],'actual-q4-model')

    def test_endpoint_accepts_any_listed_name_in_preference_order(self):
        # One port, two engines: one serves an alias, the other only the canonical name.
        config = self.config()
        config["local_model_endpoints"][0]["models"] = ["canonical-q4", "other-q4"]
        payload = {"data": [{"id": "other-q4", "status": {"value": "loaded"}},
                            {"id": "canonical-q4", "status": {"value": "loaded"}}]}
        with patch("local_llm.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())) as fetch:
            selected = resolve_local_model(config)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(selected["base_url"], "http://127.0.0.1:18081/v1")
        self.assertEqual(selected["model"], "canonical-q4")

    def test_configured_model_stays_preferred_over_listed_names(self):
        config = self.config()
        config["local_model_endpoints"][0]["models"] = ["canonical-q4"]
        payload = {"data": [{"id": "canonical-q4"}, {"id": "preferred"}]}
        with patch("local_llm.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())):
            self.assertEqual(resolve_local_model(config)["model"], "preferred")

    def test_models_list_alone_is_enough(self):
        config = self.config()
        del config["local_model_endpoints"][0]["model"]
        config["local_model_endpoints"][0]["models"] = ["canonical-q4"]
        with patch("local_llm.urllib.request.urlopen", return_value=self.response("canonical-q4")):
            self.assertEqual(resolve_local_model(config)["model"], "canonical-q4")

    def test_invalid_model_lists_rejected_before_network(self):
        for models in ("canonical-q4", [""], [3], None):
            with self.subTest(models=models):
                config = self.config()
                config["local_model_endpoints"][0]["models"] = models
                with patch("local_llm.urllib.request.urlopen") as fetch:
                    with self.assertRaises(ConfigError): resolve_local_model(config)
                    fetch.assert_not_called()
        config = self.config()
        del config["local_model_endpoints"][0]["model"]
        with self.assertRaises(ConfigError): resolve_local_model(config)

    def test_unmatched_name_error_says_what_the_server_reports(self):
        with patch("local_llm.urllib.request.urlopen", side_effect=[self.response("canonical-q4"), OSError("closed")]):
            with self.assertRaises(ServiceError) as error:
                resolve_local_model(self.config())
        message = str(error.exception)
        self.assertIn("18081 提供 canonical-q4", message)
        self.assertIn("配置接受 preferred", message)
        self.assertIn("18080 离线", message)
        self.assertIn("models", message)
        self.assertNotIn("private-key", message)


if __name__ == "__main__":
    unittest.main()
