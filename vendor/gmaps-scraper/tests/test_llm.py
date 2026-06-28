from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.error
from io import BytesIO
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from gmaps_scraper import llm as llm_module
from gmaps_scraper.llm import (
    LLMRepairError,
    cached_place_repairer,
    openai_compatible_place_repair,
    openai_compatible_place_repairer_from_env,
)
from gmaps_scraper.models import (
    PLACE_LLM_REPAIR_FIELDS,
    PlaceExtractionDiagnostics,
    PlaceLLMRepairRequest,
)


class _FakeHTTPResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload
        self.status = 200

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self) -> _FakeHTTPResponse:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


def _build_request() -> PlaceLLMRepairRequest:
    return PlaceLLMRepairRequest(
        source_url="https://www.google.com/maps/place/Den",
        resolved_url="https://www.google.com/maps/place/Den",
        current_fields={"name": "Den"},
        diagnostics=PlaceExtractionDiagnostics(quality_flags=["missing_address"]),
        evidence={"dom_excerpt": "Den"},
    )


def _build_address_request(address: str, evidence_hash: str) -> PlaceLLMRepairRequest:
    return PlaceLLMRepairRequest(
        source_url=f"https://www.google.com/maps/place/{evidence_hash}",
        resolved_url=f"https://www.google.com/maps/place/{evidence_hash}",
        current_fields={"name": "Fixture", "address": address},
        diagnostics=PlaceExtractionDiagnostics(
            quality_flags=["needs_address_display_en"],
            evidence_hash=evidence_hash,
            prompt_version="prompt-fixture",
        ),
        evidence={"dom_excerpt": address},
    )


class LLMConfigTests(unittest.TestCase):
    def test_cached_place_repairer_reuses_evidence_hash_results(self) -> None:
        calls: list[PlaceLLMRepairRequest] = []

        def repairer(request: PlaceLLMRepairRequest) -> dict[str, object]:
            calls.append(request)
            return {"address_display_en": "Tokyo, Japan"}

        request = _build_request()
        request.diagnostics.evidence_hash = "evidence-fixture"
        request.diagnostics.prompt_version = "prompt-fixture"

        with tempfile.TemporaryDirectory() as tmp_dir:
            cached = cached_place_repairer(
                repairer,
                cache_dir=Path(tmp_dir),
                cache_namespace="gpt-test",
            )

            first = cached(request)
            second = cached(request)

        self.assertEqual(first, {"address_display_en": "Tokyo, Japan", "_repair_source": "llm"})
        self.assertEqual(
            second,
            {"address_display_en": "Tokyo, Japan", "_repair_source": "cache"},
        )
        self.assertEqual(len(calls), 1)

    def test_cached_place_repairer_separates_llm_task_namespaces(self) -> None:
        calls: list[PlaceLLMRepairRequest] = []

        def repairer(request: PlaceLLMRepairRequest) -> dict[str, object]:
            calls.append(request)
            return {"address_display_en": "Tokyo, Japan"}

        first_request = _build_request()
        first_request.tasks = ["display_translation"]
        first_request.diagnostics.evidence_hash = "evidence-fixture"
        first_request.diagnostics.prompt_version = "prompt-fixture"
        second_request = _build_request()
        second_request.tasks = ["dom_repair"]
        second_request.diagnostics.evidence_hash = "evidence-fixture"
        second_request.diagnostics.prompt_version = "prompt-fixture"

        with tempfile.TemporaryDirectory() as tmp_dir:
            cached = cached_place_repairer(
                repairer,
                cache_dir=Path(tmp_dir),
                cache_namespace="gpt-test",
            )

            cached(first_request)
            cached(second_request)

        self.assertEqual(len(calls), 2)

    def test_cached_place_repairer_reuses_learned_translation_memory(self) -> None:
        calls: list[PlaceLLMRepairRequest] = []

        def repairer(request: PlaceLLMRepairRequest) -> dict[str, object]:
            calls.append(request)
            return {"address_display_en": "Seoul, Gangnam-gu"}

        with tempfile.TemporaryDirectory() as tmp_dir:
            cached = cached_place_repairer(
                repairer,
                cache_dir=Path(tmp_dir),
                cache_namespace="gpt-test",
            )

            first = cached(_build_address_request("서울시, 강남구", "evidence-one"))
            second = cached(_build_address_request("서울시, 강남구", "evidence-two"))

        self.assertEqual(
            first,
            {"address_display_en": "Seoul, Gangnam-gu", "_repair_source": "llm"},
        )
        self.assertEqual(
            second,
            {
                "fields": {
                    "address_display_en": "Seoul, Gangnam-gu",
                    "address_display_en_source": "translation_memory",
                    "address_display_en_confidence": "medium",
                },
                "_repair_source": "translation_memory",
            },
        )
        self.assertEqual(len(calls), 1)

    def test_cached_translation_memory_hit_does_not_require_model_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_dir = Path(tmp_dir)
            (cache_dir / "translation-memory.learned.json").write_text(
                json.dumps(
                    {
                        "entries": [
                            {
                                "source": "서울시, 강남구",
                                "target": "Seoul, Gangnam-gu",
                                "field_kinds": ["address"],
                                "source_method": "learned",
                                "confidence": "medium",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                repairer = openai_compatible_place_repairer_from_env(
                    env_file=cache_dir / ".env",
                    default_config_file=cache_dir / "llm.json",
                    local_config_file=cache_dir / "llm.local.json",
                )
                cached = cached_place_repairer(
                    repairer,
                    cache_dir=cache_dir,
                    cache_namespace="gpt-test",
                )
                result = cached(_build_address_request("서울시, 강남구", "evidence-one"))

        self.assertEqual(
            result,
            {
                "fields": {
                    "address_display_en": "Seoul, Gangnam-gu",
                    "address_display_en_source": "translation_memory",
                    "address_display_en_confidence": "medium",
                },
                "_repair_source": "translation_memory",
            },
        )

    def test_gpt_5_mini_uses_checked_in_defaults_and_omits_temperature(self) -> None:
        captured: dict[str, object] = {}

        def fake_urlopen(http_request: object, timeout: float) -> _FakeHTTPResponse:
            del timeout
            request_data = json.loads(http_request.data.decode("utf-8"))
            captured["url"] = http_request.full_url
            captured["payload"] = request_data
            return _FakeHTTPResponse(
                {"choices": [{"message": {"content": "{}"}}]}
            )

        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            env_path.write_text(
                "OPENAI_API_KEY=test-openai-key\nLLM_MODEL=gpt-5-mini\n",
                encoding="utf-8",
            )
            local_config_path = Path(tmp_dir) / ".llm.local.json"
            with (
                patch.dict("os.environ", {}, clear=True),
                patch("urllib.request.urlopen", side_effect=fake_urlopen),
            ):
                repair = openai_compatible_place_repairer_from_env(
                    env_file=env_path,
                    local_config_file=local_config_path,
                )
                response = repair(_build_request())

        self.assertEqual(response, {})
        self.assertEqual(captured["url"], "https://api.openai.com/v1/chat/completions")
        payload = captured["payload"]
        self.assertEqual(payload["model"], "gpt-5-mini")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertNotIn("temperature", payload)
        messages = payload["messages"]
        self.assertIsInstance(messages, list)
        user_message = messages[1]
        self.assertIsInstance(user_message, dict)
        content = user_message["content"]
        self.assertIsInstance(content, str)
        self.assertEqual(
            json.loads(content)["allowed_fields"],
            list(PLACE_LLM_REPAIR_FIELDS),
        )

    def test_unknown_llm_task_falls_back_to_all_allowed_fields(self) -> None:
        captured: dict[str, object] = {}

        def fake_urlopen(http_request: object, timeout: float) -> _FakeHTTPResponse:
            del timeout
            captured["payload"] = json.loads(http_request.data.decode("utf-8"))
            return _FakeHTTPResponse(
                {"choices": [{"message": {"content": "{}"}}]}
            )

        request = _build_request()
        request.tasks = ["future_task"]
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            response = openai_compatible_place_repair(
                request,
                api_key="test-openai-key",
                base_url="https://api.openai.com/v1",
                model="gpt-5-mini",
            )

        self.assertEqual(response, {})
        messages = captured["payload"]["messages"]
        self.assertIsInstance(messages, list)
        user_message = messages[1]
        self.assertIsInstance(user_message, dict)
        content = user_message["content"]
        self.assertIsInstance(content, str)
        self.assertEqual(
            json.loads(content)["allowed_fields"],
            list(PLACE_LLM_REPAIR_FIELDS),
        )

    def test_openai_compatible_place_repair_logs_langfuse_generation(self) -> None:
        class FakeObservation:
            def __init__(self) -> None:
                self.updates: list[dict[str, object]] = []

            def update(self, **kwargs: object) -> None:
                self.updates.append(kwargs)

        class FakeManager:
            def __init__(self, observation: FakeObservation) -> None:
                self.observation = observation
                self.closed = False

            def __enter__(self) -> FakeObservation:
                return self.observation

            def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
                self.closed = True
                return False

        class FakeClient:
            def __init__(self) -> None:
                self.observation = FakeObservation()
                self.manager = FakeManager(self.observation)
                self.started: dict[str, object] | None = None

            def start_as_current_observation(self, **kwargs: object) -> FakeManager:
                self.started = kwargs
                return self.manager

        fake_client = FakeClient()

        def fake_urlopen(http_request: object, timeout: float) -> _FakeHTTPResponse:
            del http_request, timeout
            return _FakeHTTPResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps({"address_display_en": "Tokyo, Japan"})
                            }
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 13,
                        "completion_tokens": 5,
                        "total_tokens": 18,
                    },
                }
            )

        with (
            patch("urllib.request.urlopen", side_effect=fake_urlopen),
            patch.object(llm_module, "_configured_langfuse_client", return_value=fake_client),
        ):
            response = openai_compatible_place_repair(
                _build_request(),
                api_key="test-openai-key",
                base_url="https://api.openai.com/v1",
                model="gpt-5-mini",
            )

        self.assertEqual(response, {"address_display_en": "Tokyo, Japan"})
        self.assertIsNotNone(fake_client.started)
        assert fake_client.started is not None
        self.assertEqual(fake_client.started["as_type"], "generation")
        self.assertEqual(fake_client.started["name"], "gmaps-scraper.place-repair")
        self.assertEqual(fake_client.started["model"], "gpt-5-mini")
        self.assertNotIn("input", fake_client.started)
        started_metadata = fake_client.started["metadata"]
        self.assertIsInstance(started_metadata, dict)
        self.assertIn("source_url_hash", started_metadata)
        self.assertIn("resolved_url_hash", started_metadata)
        self.assertNotIn("source_url", started_metadata)
        self.assertNotIn("resolved_url", started_metadata)
        self.assertTrue(fake_client.manager.closed)
        self.assertEqual(
            fake_client.observation.updates[-1]["usage_details"],
            {"input_tokens": 13, "output_tokens": 5, "total_tokens": 18},
        )
        self.assertNotIn("output", fake_client.observation.updates[-1])
        self.assertEqual(
            fake_client.observation.updates[-1]["metadata"],
            {**fake_client.started["metadata"], "status": "success"},
        )

    def test_openai_compatible_place_repair_logs_without_usage(self) -> None:
        class FakeObservation:
            def __init__(self) -> None:
                self.updates: list[dict[str, object]] = []

            def update(self, **kwargs: object) -> None:
                self.updates.append(kwargs)

        class FakeManager:
            def __init__(self, observation: FakeObservation) -> None:
                self.observation = observation
                self.closed = False

            def __enter__(self) -> FakeObservation:
                return self.observation

            def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
                self.closed = True
                return False

        class FakeClient:
            def __init__(self) -> None:
                self.observation = FakeObservation()
                self.manager = FakeManager(self.observation)
                self.started: dict[str, object] | None = None

            def start_as_current_observation(self, **kwargs: object) -> FakeManager:
                self.started = kwargs
                return self.manager

        fake_client = FakeClient()

        def fake_urlopen(http_request: object, timeout: float) -> _FakeHTTPResponse:
            del http_request, timeout
            return _FakeHTTPResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps({"address_display_en": "Tokyo, Japan"})
                            }
                        }
                    ],
                }
            )

        with (
            patch("urllib.request.urlopen", side_effect=fake_urlopen),
            patch.object(llm_module, "_configured_langfuse_client", return_value=fake_client),
        ):
            response = openai_compatible_place_repair(
                _build_request(),
                api_key="test-openai-key",
                base_url="https://api.openai.com/v1",
                model="gpt-5-mini",
            )

        self.assertEqual(response, {"address_display_en": "Tokyo, Japan"})
        self.assertIsNotNone(fake_client.started)
        assert fake_client.started is not None
        self.assertEqual(fake_client.started["as_type"], "generation")
        self.assertEqual(fake_client.started["name"], "gmaps-scraper.place-repair")
        self.assertEqual(fake_client.started["model"], "gpt-5-mini")
        self.assertTrue(fake_client.manager.closed)
        self.assertNotIn("usage_details", fake_client.observation.updates[-1])
        self.assertEqual(
            fake_client.observation.updates[-1]["metadata"],
            {**fake_client.started["metadata"], "status": "success"},
        )

    def test_openai_compatible_place_repair_redacts_http_error_body(self) -> None:
        class FakeObservation:
            def __init__(self) -> None:
                self.updates: list[dict[str, object]] = []

            def update(self, **kwargs: object) -> None:
                self.updates.append(kwargs)

        class FakeManager:
            def __init__(self, observation: FakeObservation) -> None:
                self.observation = observation
                self.closed = False

            def __enter__(self) -> FakeObservation:
                return self.observation

            def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
                self.closed = True
                return False

        class FakeClient:
            def __init__(self) -> None:
                self.observation = FakeObservation()
                self.manager = FakeManager(self.observation)

            def start_as_current_observation(self, **kwargs: object) -> FakeManager:
                del kwargs
                return self.manager

        fake_client = FakeClient()

        def fake_urlopen(http_request: object, timeout: float) -> object:
            del timeout
            raise urllib.error.HTTPError(
                url=http_request.full_url,
                code=400,
                msg="Bad Request",
                hdrs={},
                fp=BytesIO(b"request payload leaked by provider"),
            )

        with (
            patch("urllib.request.urlopen", side_effect=fake_urlopen),
            patch.object(llm_module, "_configured_langfuse_client", return_value=fake_client),
            self.assertRaises(LLMRepairError) as raised,
        ):
            openai_compatible_place_repair(
                _build_request(),
                api_key="test-openai-key",
                base_url="https://api.openai.com/v1",
                model="gpt-5-mini",
            )

        self.assertEqual(str(raised.exception), "LLM repair HTTP 400")
        self.assertTrue(fake_client.manager.closed)
        metadata = fake_client.observation.updates[-1]["metadata"]
        self.assertIsInstance(metadata, dict)
        self.assertEqual(metadata["status"], "http_error")
        self.assertEqual(metadata["status_code"], 400)
        self.assertNotIn("error", metadata)

    def test_langfuse_client_uses_base_url_from_env(self) -> None:
        with (
            patch.dict(
                "os.environ",
                {
                    "LANGFUSE_PUBLIC_KEY": "pk-lf-test",
                    "LANGFUSE_SECRET_KEY": "sk-lf-test",
                    "LANGFUSE_BASE_URL": "https://us.cloud.langfuse.com",
                },
                clear=True,
            )
        ):
            config = llm_module._langfuse_config_from_env()

        self.assertEqual(config, ("pk-lf-test", "sk-lf-test", "https://us.cloud.langfuse.com"))

    def test_langfuse_client_uses_host_fallback_from_env(self) -> None:
        with (
            patch.dict(
                "os.environ",
                {
                    "LANGFUSE_PUBLIC_KEY": "pk-lf-test",
                    "LANGFUSE_SECRET_KEY": "sk-lf-test",
                    "LANGFUSE_HOST": "eu.langfuse.com",
                },
                clear=True,
            )
        ):
            config = llm_module._langfuse_config_from_env()

        self.assertEqual(config, ("pk-lf-test", "sk-lf-test", "https://eu.langfuse.com"))

    def test_langfuse_client_does_not_cache_disabled_env(self) -> None:
        class FakeLangfuse:
            def __init__(self, **kwargs: object) -> None:
                self.kwargs = kwargs

        fake_module = ModuleType("langfuse")
        fake_module.Langfuse = FakeLangfuse  # type: ignore[attr-defined]
        llm_module._clear_langfuse_client_cache()
        self.addCleanup(llm_module._clear_langfuse_client_cache)

        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(llm_module._configured_langfuse_client())

        with (
            patch.dict(
                "os.environ",
                {
                    "LANGFUSE_PUBLIC_KEY": "pk-lf-test",
                    "LANGFUSE_SECRET_KEY": "sk-lf-test",
                },
                clear=True,
            ),
            patch.dict(sys.modules, {"langfuse": fake_module}),
        ):
            client = llm_module._configured_langfuse_client()

        self.assertIsInstance(client, FakeLangfuse)
        self.assertEqual(
            client.kwargs,
            {"public_key": "pk-lf-test", "secret_key": "sk-lf-test"},
        )

    def test_local_config_can_define_fireworks_alias(self) -> None:
        captured: dict[str, object] = {}

        def fake_urlopen(http_request: object, timeout: float) -> _FakeHTTPResponse:
            del timeout
            captured["url"] = http_request.full_url
            captured["payload"] = json.loads(http_request.data.decode("utf-8"))
            return _FakeHTTPResponse(
                {"choices": [{"message": {"content": "{}"}}]}
            )

        local_config = {
            "providers": {
                "fireworks": {
                    "api_key_env": "FIREWORKS_API_KEY",
                    "base_url": "https://api.fireworks.ai/inference/v1",
                }
            },
            "models": {
                "kimi-k2p6": {
                    "provider": "fireworks",
                    "model": "accounts/fireworks/models/kimi-k2p6",
                    "omit_temperature": True,
                    "request_options": {
                        "reasoning_effort": "low",
                        "temperature": 0,
                    },
                }
            },
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            env_path.write_text(
                "FIREWORKS_API_KEY=test-fireworks-key\nLLM_MODEL=kimi-k2p6\n",
                encoding="utf-8",
            )
            local_config_path = Path(tmp_dir) / ".llm.local.json"
            local_config_path.write_text(json.dumps(local_config), encoding="utf-8")
            with (
                patch.dict("os.environ", {}, clear=True),
                patch("urllib.request.urlopen", side_effect=fake_urlopen),
            ):
                repair = openai_compatible_place_repairer_from_env(
                    env_file=env_path,
                    local_config_file=local_config_path,
                )
                response = repair(_build_request())

        self.assertEqual(response, {})
        self.assertEqual(
            captured["url"],
            "https://api.fireworks.ai/inference/v1/chat/completions",
        )
        payload = captured["payload"]
        self.assertEqual(payload["model"], "accounts/fireworks/models/kimi-k2p6")
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertNotIn("temperature", payload)

    def test_env_vars_override_local_request_options(self) -> None:
        captured: dict[str, object] = {}

        def fake_urlopen(http_request: object, timeout: float) -> _FakeHTTPResponse:
            del timeout
            captured["payload"] = json.loads(http_request.data.decode("utf-8"))
            return _FakeHTTPResponse(
                {"choices": [{"message": {"content": "{}"}}]}
            )

        local_config = {
            "models": {
                "gpt-4o-mini": {
                    "provider": "openai",
                    "model": "gpt-4o-mini",
                    "request_options": {
                        "temperature": 0.2,
                        "max_tokens": 256,
                    },
                }
            }
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            env_path.write_text(
                "OPENAI_API_KEY=test-openai-key\nLLM_MODEL=gpt-4o-mini\n",
                encoding="utf-8",
            )
            local_config_path = Path(tmp_dir) / ".llm.local.json"
            local_config_path.write_text(json.dumps(local_config), encoding="utf-8")
            with (
                patch.dict(
                    "os.environ",
                    {
                        "LLM_TEMPERATURE": "0.7",
                        "LLM_MAX_TOKENS": "128",
                    },
                    clear=True,
                ),
                patch("urllib.request.urlopen", side_effect=fake_urlopen),
            ):
                repair = openai_compatible_place_repairer_from_env(
                    env_file=env_path,
                    local_config_file=local_config_path,
                )
                response = repair(_build_request())

        self.assertEqual(response, {})
        payload = captured["payload"]
        self.assertEqual(payload["temperature"], 0.7)
        self.assertEqual(payload["max_tokens"], 128)


if __name__ == "__main__":
    unittest.main()
