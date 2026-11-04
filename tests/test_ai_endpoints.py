import httpx
import pytest
from pydantic import ValidationError

from excel_auditor.workflow.ai import AIClient, AISettings
from excel_auditor.workflow.models import WorkflowError


@pytest.mark.parametrize("environment,enabled,allowed", [
    ("development", False, False), ("development", True, True),
    ("test", True, True), ("production", True, False), ("prod", True, False),
])
def test_http_is_explicitly_opted_in_and_never_allowed_in_production(monkeypatch, environment, enabled, allowed):
    monkeypatch.setenv("EXCEL_AUDITOR_ENV", environment)
    monkeypatch.setenv("EXCEL_AUDITOR_ALLOW_INSECURE_AI_HTTP", "1" if enabled else "0")
    if allowed:
        assert AISettings(base_url="http://models.example:18080/v1").base_url.startswith("http://")
    else:
        with pytest.raises(ValidationError, match="HTTP"):
            AISettings(base_url="http://models.example:18080/v1")
    assert AISettings(base_url=" https://models.example/v1/ ").base_url == "https://models.example/v1"


@pytest.mark.parametrize("url", ["ftp://models.example/v1", "http://user:password@models.example/v1",
    "http://models.example/v1?key=private", "http://models.example/v1#fragment",
    "http://models.example:0/v1", "http://models.example:99999/v1", "not-a-url"])
def test_http_compatibility_does_not_relax_url_shape(monkeypatch, url):
    monkeypatch.setenv("EXCEL_AUDITOR_ENV", "development")
    monkeypatch.setenv("EXCEL_AUDITOR_ALLOW_INSECURE_AI_HTTP", "1")
    with pytest.raises(ValidationError):
        AISettings(base_url=url)


def test_http_model_list_and_chat_work_without_exposing_key(tmp_path, monkeypatch):
    monkeypatch.setenv("EXCEL_AUDITOR_ENV", "development")
    monkeypatch.setenv("EXCEL_AUDITOR_ALLOW_INSECURE_AI_HTTP", "1")
    requests = []
    def handle(request):
        requests.append((request.method, request.url.path))
        assert request.headers["Authorization"] == "Bearer test-only-key"
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "test-model"}]})
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"mappings":[]}'}}]})

    client = AIClient(tmp_path, "local", httpx.MockTransport(handle))
    public = client.save(AISettings(base_url="http://models.example:18080/v1", model="test-model", api_key="test-only-key"))
    assert public["http_allowed"] and public["has_key"] and public["configured"]
    assert "api_key" not in public and "secret" not in public
    assert client.models() == {"models": ["test-model"]}
    assert client.reconcile_field_mappings([{"id": "name"}], [{"column": 1, "header": "Name"}]) == []
    assert requests == [("GET", "/v1/models"), ("POST", "/v1/chat/completions")]

    monkeypatch.setenv("EXCEL_AUDITOR_ALLOW_INSECURE_AI_HTTP", "0")
    assert not client.settings()["configured"]
    with pytest.raises(WorkflowError, match="安全策略"):
        client.models()
    assert len(requests) == 2


def test_production_blocks_previously_saved_http_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("EXCEL_AUDITOR_ENV", "development")
    monkeypatch.setenv("EXCEL_AUDITOR_ALLOW_INSECURE_AI_HTTP", "1")
    client = AIClient(tmp_path, "local", httpx.MockTransport(lambda request: pytest.fail("must not transmit credentials")))
    client.save(AISettings(base_url="http://models.example/v1", model="test-model", api_key="test-only-key"))
    monkeypatch.setenv("EXCEL_AUDITOR_ENV", "production")
    assert not client.settings()["http_allowed"] and not client.settings()["configured"]
    with pytest.raises(WorkflowError, match="安全策略"):
        client.models()


@pytest.mark.parametrize("failure", ["timeout", "connection", 429, 503])
def test_header_analysis_recovers_from_transient_failure(tmp_path, monkeypatch, caplog, failure):
    from excel_auditor.workflow import mapping_runtime as runtime_module
    from excel_auditor.workflow.mapping_runtime import MappingRuntime

    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setattr(runtime_module.time, "sleep", lambda _: None)
    requests = []

    def handle(request):
        requests.append(request)
        assert request.extensions["timeout"]["read"] == 120
        assert request.extensions["timeout"]["connect"] == 10
        if len(requests) == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("private-test-key upstream body", request=request)
            if failure == "connection":
                raise httpx.ConnectError("private-test-key upstream body", request=request)
            return httpx.Response(failure, text="private-test-key upstream body")
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content":
            '{"mappings":[{"column":1,"field_id":"name","confidence":0.99,"reason":"Name"}]}'}}]})

    client = AIClient(tmp_path, "local", httpx.MockTransport(handle))
    client.save(AISettings(base_url="https://models.example/v1", model="test-model", api_key="private-test-key"))
    runtime = MappingRuntime()
    try:
        batches = [([{"id": "name"}], [{"column": 1, "header": "Product"}])]
        result = runtime.analyze(client, batches, namespace="retry-test")
        assert result[0][0]["field_id"] == "name"
        assert runtime.analyze(client, batches, namespace="retry-test") == result
        assert len(requests) == 2
        assert "retry=True" in caplog.text
        assert "private-test-key" not in caplog.text
        assert "upstream body" not in caplog.text
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("failure,expected_code,attempts", [
    ("timeout", "AI_TIMEOUT", 2), (503, "AI_SERVICE_ERROR", 2),
    (401, "AI_REQUEST_REJECTED", 1), ("invalid", "AI_INVALID_MAPPING", 2),
])
def test_header_analysis_bounds_retries_and_preserves_failure_reason(tmp_path, monkeypatch, failure, expected_code, attempts):
    from excel_auditor.workflow import mapping_runtime as runtime_module
    from excel_auditor.workflow.mapping_runtime import MappingRuntime

    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setattr(runtime_module.time, "sleep", lambda _: None)
    requests = []

    def handle(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private-test-key", request=request)
        if failure == "invalid":
            return httpx.Response(200, json={"choices": [{"message": {"content": "invalid"}}]})
        return httpx.Response(failure, text="private-test-key")

    client = AIClient(tmp_path, "local", httpx.MockTransport(handle))
    client.save(AISettings(base_url="https://models.example/v1", model="test-model", api_key="private-test-key"))
    runtime = MappingRuntime()
    try:
        result = runtime.analyze(client, [([{"id": "name"}], [{"column": 1, "header": "Product"}])], namespace="failure")
        assert isinstance(result[0], WorkflowError)
        assert result[0].code == expected_code
        assert len(requests) == attempts
        assert "private-test-key" not in str(result[0])
    finally:
        runtime.shutdown()


def test_header_analysis_recovers_after_one_malformed_model_response(tmp_path, monkeypatch):
    from excel_auditor.workflow import mapping_runtime as runtime_module
    from excel_auditor.workflow.mapping_runtime import MappingRuntime

    monkeypatch.delenv('REDIS_URL', raising=False)
    monkeypatch.setattr(runtime_module.time, 'sleep', lambda _: None)
    calls = []

    def handle(request):
        calls.append(request)
        content = 'invalid' if len(calls) == 1 else (
            '{"mappings":[{"column":1,"field_id":"name","confidence":0.95,"reason":"表头同义"}]}'
        )
        return httpx.Response(200, json={'choices': [{'message': {'content': content}}]})

    client = AIClient(tmp_path, 'local', httpx.MockTransport(handle))
    client.save(AISettings(base_url='https://models.example/v1', model='test-model', api_key='private-test-key'))
    runtime = MappingRuntime()
    try:
        result = runtime.analyze(client, [([{'id': 'name'}], [{'column': 1, 'header': '商品标题'}])],
                                 namespace='malformed-retry')
        assert result == [[{'column': 1, 'field_id': 'name', 'confidence': 0.95, 'reason': '表头同义'}]]
        assert len(calls) == 2
    finally:
        runtime.shutdown()


def test_duplicate_model_target_drops_only_ambiguous_suggestion(tmp_path):
    def handle(request):
        return httpx.Response(200, json={'choices': [{'message': {'content':
            '{"mappings":['
            '{"column":1,"field_id":"brand","confidence":0.9,"reason":"品牌"},'
            '{"column":2,"field_id":"brand","confidence":0.9,"reason":"厂牌"},'
            '{"column":3,"field_id":"sku","confidence":0.95,"reason":"货号"}'
            ']}'}}]})

    client = AIClient(tmp_path, 'local', httpx.MockTransport(handle))
    client.save(AISettings(base_url='https://models.example/v1', model='test-model', api_key='private-test-key'))
    result = client.reconcile_field_mappings(
        [{'id': 'brand'}, {'id': 'sku'}],
        [{'column': 1}, {'column': 2}, {'column': 3}], multiple_targets=True)
    assert result == [{'column': 3, 'field_id': 'sku', 'confidence': 0.95, 'reason': '货号'}]
