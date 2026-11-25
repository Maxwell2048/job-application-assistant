"""LangChain transport for the local OpenAI-compatible model service.

Selected with ``"adapter": "langchain_openai"`` in config.local.json. The
request goes through LangChain's ``ChatOpenAI`` instead of urllib, while the
attempt budget, schema checks, quote validation and correction messages stay
in ``local_llm.BaseAdapter``: the framework sends and receives, the program
still decides what counts as a valid answer.

LangSmith tracing would upload prompts, and prompts carry personal source
text, so the adapter refuses to start while tracing is switched on.
"""
import os

from local_llm import (ConfigError, OpenAIHttpAdapter, RequestRejectedError, RequestTimeoutError, ServiceError,
                       StructureError, register_adapter)

TRACING_VARIABLES = ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING")
_ROLES = {"system": "SystemMessage", "user": "HumanMessage", "assistant": "AIMessage"}


def assert_tracing_off(environ=None):
    """Raise ConfigError when any LangSmith tracing switch is on."""
    environ = os.environ if environ is None else environ
    for name in TRACING_VARIABLES:
        if str(environ.get(name, "")).strip().lower() in ("1", "true", "yes", "on"):
            raise ConfigError(f"{name} 已开启：LangSmith 追踪会把提示词（含个人材料）上传到云端；关闭后再运行")


def _text(content):
    """Message content as text; a list of parts keeps only the text parts."""
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content
                       if isinstance(part, dict) and part.get("type") in ("text", "output_text")
                       and isinstance(part.get("text"), str))
    return content if isinstance(content, str) else ""


@register_adapter
class LangChainOpenAIAdapter(OpenAIHttpAdapter):
    """Same endpoint, request settings and errors as ``openai_http``, sent through LangChain."""
    name = "langchain_openai"

    def __init__(self, config):
        super().__init__(config)
        assert_tracing_off()
        try:
            import langchain_core.messages as messages
            from langchain_openai import ChatOpenAI
            import openai
        except ImportError as exc:
            raise ConfigError("langchain_openai 适配器需要 langchain-openai；请运行 "
                              "python -m pip install -r requirements.txt") from exc
        self._messages, self._chat_class, self._openai = messages, ChatOpenAI, openai

    def describe_parameters(self):
        from importlib.metadata import PackageNotFoundError, version
        try:
            client = "langchain-openai " + version("langchain-openai")
        except PackageNotFoundError:
            client = "langchain-openai"
        return {**super().describe_parameters(), "client": client}

    def chat_model(self, timeout):
        """The LangChain chat model for one request. LangChain sends the output
        limit as max_completion_tokens, which some local servers ignore, so the
        classic max_tokens field goes along in the body as well."""
        base = self._endpoint()[: -len("/chat/completions")]
        extra = {"max_tokens": self.max_output_tokens, **self.extra_body}
        return self._chat_class(model=self.model, base_url=base, api_key=self.api_key or "local",
                                temperature=self.temperature, max_tokens=self.max_output_tokens, timeout=timeout,
                                max_retries=0, extra_body=extra)

    def _response_format(self, schema):
        if self.response_format == "json_schema":
            return {"type": "json_schema", "json_schema": {"name": "result", "strict": False, "schema": schema or {}}}
        if self.response_format == "json_object":
            return {"type": "json_object"}
        return None

    def _redact(self, text):
        text = " ".join(str(text).split())[:600]
        return text.replace(self.api_key, "[redacted]") if self.api_key else text

    def _call_service(self, messages, timeout, schema):
        lc_messages = [getattr(self._messages, _ROLES[m["role"]])(content=m["content"]) for m in messages]
        kwargs = {}
        response_format = self._response_format(schema)
        if response_format:
            kwargs["response_format"] = response_format
        openai = self._openai
        truncated = StructureError(
            f"模型输出在 {self.max_output_tokens} 个输出 token 处被截断（finish_reason=length）；"
            "请输出紧凑 JSON（不缩进、不换行）并精简内容，或调大 max_output_tokens")
        try:
            result = self.chat_model(timeout).invoke(lc_messages, **kwargs)
        except openai.LengthFinishReasonError as exc:
            # With a response_format, LangChain parses through the OpenAI SDK, which raises on truncation.
            self.last_raw_response = (exc.completion.choices[0].message.content or "") if exc.completion.choices else ""
            raise truncated from exc
        except openai.APITimeoutError as exc:
            raise RequestTimeoutError(f"本机服务请求超时（{timeout} 秒）", error_type="timeout") from exc
        except openai.APIStatusError as exc:
            body = exc.body if isinstance(exc.body, dict) else {}
            error = body.get("error", body) if isinstance(body, dict) else {}
            kind = str(error.get("code") or error.get("type") or "") if isinstance(error, dict) else ""
            detail = self._redact(error.get("message", "") if isinstance(error, dict) else exc.message)
            message = f"本机服务返回 HTTP {exc.status_code}" + (f"；{detail}" if detail else "")
            if kind == "structured_output_failed":
                raise StructureError("模型输出结构错误：" + message) from exc
            if exc.status_code == 400:
                message += "；请核对 model、response_format、extra_body 与服务支持的参数"
            rejected = 400 <= exc.status_code < 500 and exc.status_code not in (408, 429)
            raise (RequestRejectedError if rejected else ServiceError)(message, error_type="http_error") from exc
        except openai.APIConnectionError as exc:
            raise ServiceError(f"本机服务不可用：{self._redact(exc)}") from exc
        content = _text(result.content)
        self.last_raw_response = content
        if not content.strip():
            raise StructureError("模型正文为空或不是文本；请检查思考模式、内容字段和服务端输出限制")
        if (result.response_metadata or {}).get("finish_reason") == "length":
            raise truncated
        return content
