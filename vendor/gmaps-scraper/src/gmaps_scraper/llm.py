"""Optional LLM repair helpers for place extraction."""

from __future__ import annotations

import atexit
import importlib
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from gmaps_scraper.models import (
    PLACE_LLM_DISPLAY_TRANSLATION_FIELDS,
    PLACE_LLM_DOM_REPAIR_FIELDS,
    PLACE_LLM_REPAIR_FIELDS,
    PlaceLLMRepairRequest,
)
from gmaps_scraper.translation_memory import (
    TranslationMemory,
    write_learned_translation_memory,
)

_DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_CONFIG_PATH = Path("llm.json")
_DEFAULT_LOCAL_CONFIG_PATH = Path("llm.local.json")
_FALLBACK_LLM_SETTINGS: dict[str, object] = {
    "providers": {
        "openai": {
            "api_key_env": "OPENAI_API_KEY",
            "base_url": _DEFAULT_OPENAI_BASE_URL,
            "base_url_env": "OPENAI_BASE_URL",
        },
    },
    "models": {
        "gpt-5-mini": {
            "provider": "openai",
            "model": "gpt-5-mini",
            "omit_temperature": True,
        },
        "gpt-4o-mini": {
            "provider": "openai",
            "model": "gpt-4o-mini",
        },
        "gpt-4.1-mini": {
            "provider": "openai",
            "model": "gpt-4.1-mini",
        },
    },
    "request_options": {
        "response_format": {
            "type": "json_object",
        }
    },
}


@dataclass(frozen=True)
class _ResolvedLLMConfig:
    api_key: str
    base_url: str
    model: str
    request_options: Mapping[str, object]


class LLMRepairError(RuntimeError):
    """Raised when an optional LLM repair call fails."""


class _NoopLangfuseObservation:
    def update(self, **_: Any) -> None:
        return None


_LANGFUSE_CLIENT_CACHE: dict[tuple[str, str, str | None], Any] = {}
_LANGFUSE_FLUSH_CLIENT_IDS: set[int] = set()
_LANGFUSE_CLIENT_CACHE_LOCK = threading.Lock()


def _configured_langfuse_client() -> Any | None:
    config = _langfuse_config_from_env()
    if config is None:
        return None
    return _langfuse_client_for_config(config)


def _langfuse_client_for_config(config: tuple[str, str, str | None]) -> Any | None:
    cached = _LANGFUSE_CLIENT_CACHE.get(config)
    if cached is not None:
        return cached
    with _LANGFUSE_CLIENT_CACHE_LOCK:
        cached = _LANGFUSE_CLIENT_CACHE.get(config)
        if cached is not None:
            return cached
        public_key, secret_key, base_url = config
        try:
            langfuse_module = importlib.import_module("langfuse")
            langfuse_class = langfuse_module.Langfuse
        except (ImportError, AttributeError):
            return None
        try:
            if base_url:
                client = langfuse_class(
                    public_key=public_key,
                    secret_key=secret_key,
                    base_url=base_url,
                )
            else:
                client = langfuse_class(public_key=public_key, secret_key=secret_key)
        except Exception:
            return None
        _LANGFUSE_CLIENT_CACHE[config] = client
        _register_langfuse_flush(client)
        return client


def _langfuse_config_from_env() -> tuple[str, str, str | None] | None:
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
    base_url = _normalize_langfuse_base_url(
        os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST")
    )
    if not public_key or not secret_key:
        return None
    return public_key, secret_key, base_url


def _clear_langfuse_client_cache() -> None:
    with _LANGFUSE_CLIENT_CACHE_LOCK:
        _LANGFUSE_CLIENT_CACHE.clear()
        _LANGFUSE_FLUSH_CLIENT_IDS.clear()


def _normalize_langfuse_base_url(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip().rstrip("/")
    if not stripped:
        return None
    if "://" in stripped:
        return stripped
    return f"https://{stripped}"


def _register_langfuse_flush(client: Any) -> None:
    client_id = id(client)
    if client_id in _LANGFUSE_FLUSH_CLIENT_IDS:
        return
    _LANGFUSE_FLUSH_CLIENT_IDS.add(client_id)
    atexit.register(_flush_langfuse_client, client)


def _flush_langfuse_client(client: Any) -> None:
    try:
        client.flush()
    except Exception:
        return


def _langfuse_full_capture_enabled() -> bool:
    return _env_flag("GMAPS_SCRAPER_LANGFUSE_FULL_CAPTURE")


def _env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _langfuse_hash(value: str | None) -> str | None:
    if not value:
        return None
    return sha256(value.encode("utf-8")).hexdigest()


def _open_langfuse_generation(
    *,
    name: str,
    model: str,
    input_payload: Mapping[str, object] | None,
    metadata: Mapping[str, object],
) -> tuple[Any, Any]:
    client = _configured_langfuse_client()
    if client is None:
        return None, _NoopLangfuseObservation()
    try:
        kwargs: dict[str, object] = {
            "as_type": "generation",
            "name": name,
            "model": model,
            "metadata": metadata,
        }
        if input_payload is not None:
            kwargs["input"] = input_payload
        manager = client.start_as_current_observation(**kwargs)
        observation = manager.__enter__()
    except Exception:
        return None, _NoopLangfuseObservation()
    return manager, observation


def _close_langfuse_generation(manager: Any, exc_info: tuple[Any, Any, Any]) -> None:
    if manager is None:
        return
    try:
        manager.__exit__(*exc_info)
    except Exception:
        return


def _update_langfuse_generation(observation: Any, **kwargs: Any) -> None:
    try:
        kwargs = {key: value for key, value in kwargs.items() if value is not None}
        observation.update(**kwargs)
    except Exception:
        return


def _openai_usage_details(response_payload: Mapping[str, Any]) -> dict[str, int] | None:
    usage = response_payload.get("usage")
    if not isinstance(usage, Mapping):
        return None
    details: dict[str, int] = {}
    for langfuse_key, openai_key in (
        ("input_tokens", "prompt_tokens"),
        ("output_tokens", "completion_tokens"),
        ("total_tokens", "total_tokens"),
    ):
        value = usage.get(openai_key)
        if isinstance(value, int):
            details[langfuse_key] = value
    return details or None


def openai_compatible_place_repairer_from_env(
    *,
    env_file: Path | None = None,
    default_config_file: Path | None = None,
    local_config_file: Path | None = None,
    timeout_seconds: float = 45.0,
) -> Callable[[PlaceLLMRepairRequest], Mapping[str, object] | None]:
    """Build a provider-neutral place repair callback from OpenAI-style env vars."""
    resolved: _ResolvedLLMConfig | None = None
    resolved_lock = threading.Lock()

    def get_resolved_config() -> _ResolvedLLMConfig:
        nonlocal resolved
        if resolved is not None:
            return resolved
        with resolved_lock:
            if resolved is None:
                resolved = _resolve_llm_config_from_env(
                    env_file=env_file or Path(".env"),
                    default_config_file=default_config_file or _DEFAULT_CONFIG_PATH,
                    local_config_file=local_config_file or _DEFAULT_LOCAL_CONFIG_PATH,
                )
            return resolved

    def repair(request: PlaceLLMRepairRequest) -> Mapping[str, object] | None:
        resolved_config = get_resolved_config()
        return openai_compatible_place_repair(
            request,
            api_key=resolved_config.api_key,
            base_url=resolved_config.base_url,
            model=resolved_config.model,
            request_options=resolved_config.request_options,
            timeout_seconds=timeout_seconds,
        )

    return repair


def cached_place_repairer(
    repairer: Callable[[PlaceLLMRepairRequest], Mapping[str, object] | None],
    *,
    cache_dir: Path,
    cache_namespace: str,
) -> Callable[[PlaceLLMRepairRequest], Mapping[str, object] | None]:
    """Wrap a place repairer with a small JSON disk cache."""

    def repair(request: PlaceLLMRepairRequest) -> Mapping[str, object] | None:
        learned_memory_path = cache_dir / "translation-memory.learned.json"
        learned_repair = _repair_from_translation_memory(request, learned_memory_path)
        if learned_repair is not None:
            return _with_repair_source(learned_repair, "translation_memory")

        cache_key = _place_repair_cache_key(request, cache_namespace=cache_namespace)
        cache_path = cache_dir / f"{cache_key}.json"
        cached = _read_cached_repair(cache_path)
        if cached is not None:
            return _with_repair_source(cached, "cache")

        result = repairer(request)
        if result is not None:
            write_learned_translation_memory(
                learned_memory_path,
                current_fields=request.current_fields,
                repair=result,
            )
            result_with_source = _with_repair_source(result, "llm")
            _write_cached_repair(
                cache_path,
                {
                    "cache_key": cache_key,
                    "cache_namespace": cache_namespace,
                    "source_url": request.source_url,
                    "resolved_url": request.resolved_url,
                    "evidence_hash": request.diagnostics.evidence_hash,
                    "prompt_version": request.diagnostics.prompt_version,
                    "repair": dict(result_with_source),
                },
            )
            return result_with_source
        return result

    return repair


def llm_cache_namespace_from_env(
    *,
    env_file: Path | None = None,
    default_config_file: Path | None = None,
    local_config_file: Path | None = None,
) -> str:
    """Return a stable cache namespace for the configured LLM model."""
    _load_env_file(env_file or Path(".env"))
    settings = _merge_config(
        _FALLBACK_LLM_SETTINGS,
        _load_json_config(default_config_file or _DEFAULT_CONFIG_PATH),
    )
    settings = _merge_config(
        settings,
        _load_json_config(local_config_file or _DEFAULT_LOCAL_CONFIG_PATH),
    )
    model_alias_or_id = os.environ.get("LLM_MODEL") or os.environ.get("OPENAI_MODEL")
    if not model_alias_or_id:
        return "default"
    model_configs = settings.get("models")
    model_config: Mapping[str, object] = {}
    if isinstance(model_configs, Mapping):
        raw_model_config = model_configs.get(model_alias_or_id)
        if isinstance(raw_model_config, Mapping):
            model_config = raw_model_config
    provider_name = (
        os.environ.get("LLM_PROVIDER")
        or _string(model_config.get("provider"))
        or "openai"
    )
    model = _string(model_config.get("model")) or model_alias_or_id
    return f"{provider_name}:{model}"


def _repair_from_translation_memory(
    request: PlaceLLMRepairRequest,
    learned_memory_path: Path,
) -> Mapping[str, object] | None:
    quality_flags = set(request.diagnostics.quality_flags)
    supported_flags = {"needs_address_display_en", "needs_category_display_en"}
    if not quality_flags or not quality_flags.issubset(supported_flags):
        return None

    memory = TranslationMemory.from_file(learned_memory_path)
    fields: dict[str, object] = {}
    category = memory.normalize_category(request.current_fields.get("category"))
    if category is not None and not request.current_fields.get("category_display_en"):
        fields["category_display_en"] = category.text
        fields["category_display_en_source"] = "translation_memory"
        fields["category_display_en_confidence"] = category.confidence
    address = memory.normalize_address(request.current_fields.get("address"))
    if address is not None and not request.current_fields.get("address_display_en"):
        fields["address_display_en"] = address.text
        fields["address_display_en_source"] = "translation_memory"
        fields["address_display_en_confidence"] = address.confidence
    if "needs_category_display_en" in quality_flags and "category_display_en" not in fields:
        return None
    if "needs_address_display_en" in quality_flags and "address_display_en" not in fields:
        return None
    return {"fields": fields} if fields else None


def _with_repair_source(
    repair: Mapping[str, object],
    repair_source: str,
) -> Mapping[str, object]:
    result = dict(repair)
    result["_repair_source"] = repair_source
    return result


def _place_repair_cache_key(
    request: PlaceLLMRepairRequest,
    *,
    cache_namespace: str,
) -> str:
    identity = {
        "namespace": cache_namespace,
        "source_url": request.source_url,
        "resolved_url": request.resolved_url,
        "google_place_id": request.current_fields.get("google_place_id"),
        "evidence_hash": request.diagnostics.evidence_hash,
        "prompt_version": request.diagnostics.prompt_version,
        "tasks": request.tasks,
    }
    return sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()


def _read_cached_repair(cache_path: Path) -> Mapping[str, object] | None:
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    repair = payload.get("repair")
    return repair if isinstance(repair, Mapping) else None


def _write_cached_repair(cache_path: Path, payload: Mapping[str, object]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    temp_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(cache_path)


def openai_compatible_place_repair(
    request: PlaceLLMRepairRequest,
    *,
    api_key: str,
    base_url: str,
    model: str,
    request_options: Mapping[str, object] | None = None,
    timeout_seconds: float = 45.0,
) -> Mapping[str, object] | None:
    """Call an OpenAI-compatible chat completions API for place field repair."""
    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    payload: dict[str, object] = {"model": model}
    if request_options is not None:
        for key, value in request_options.items():
            if value is not None:
                payload[key] = value
    payload["messages"] = [
        {
            "role": "system",
            "content": (
                "You repair Google Maps place extraction from sanitized DOM evidence. "
                "Return only JSON. Only include fields directly supported by evidence. "
                "Do not infer product-specific tags, neighborhoods, or marketing prose. "
                "Never translate review topic labels. Do not return user-generated "
                "review text."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "allowed_fields": _allowed_fields_for_tasks(request.tasks),
                    "review_topic_shape": {
                        "label": "string",
                        "count": "integer or null",
                    },
                    "about_section_shape": {
                        "title": "string",
                        "items": [
                            {
                                "label": "string",
                                "aria_label": "string or null",
                            }
                        ],
                    },
                    "request": request.to_dict(),
                },
                ensure_ascii=False,
            ),
        },
    ]
    http_request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    full_langfuse_capture = _langfuse_full_capture_enabled()
    langfuse_metadata = {
        "base_url": base_url,
        "prompt_version": request.diagnostics.prompt_version,
        "evidence_hash": request.diagnostics.evidence_hash,
        "task_count": len(request.tasks),
        "tasks": list(request.tasks),
        "source_url_hash": _langfuse_hash(request.source_url),
        "resolved_url_hash": _langfuse_hash(request.resolved_url),
        "full_capture": full_langfuse_capture,
    }
    manager, generation = _open_langfuse_generation(
        name="gmaps-scraper.place-repair",
        model=model,
        input_payload=payload if full_langfuse_capture else None,
        metadata=langfuse_metadata,
    )
    try:
        try:
            with urllib.request.urlopen(http_request, timeout=timeout_seconds) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            metadata: dict[str, object] = {
                **langfuse_metadata,
                "status": "http_error",
                "status_code": exc.code,
            }
            if full_langfuse_capture:
                metadata["error"] = body
            _update_langfuse_generation(
                generation,
                metadata=metadata,
            )
            message = f"LLM repair HTTP {exc.code}"
            if full_langfuse_capture:
                message = f"{message}: {body}"
            raise LLMRepairError(message) from exc
        except (OSError, json.JSONDecodeError) as exc:
            _update_langfuse_generation(
                generation,
                metadata={**langfuse_metadata, "status": "error", "error": str(exc)},
            )
            raise LLMRepairError(f"LLM repair failed: {exc}") from exc

        content = _extract_chat_content(response_payload)
        if content is None:
            _update_langfuse_generation(
                generation,
                metadata={**langfuse_metadata, "status": "missing_content"},
            )
            return None
        try:
            decoded = _decode_json_object(content)
        except json.JSONDecodeError:
            _update_langfuse_generation(
                generation,
                output=content if full_langfuse_capture else None,
                metadata={**langfuse_metadata, "status": "invalid_json"},
                usage_details=_openai_usage_details(response_payload),
            )
            raise
        if not isinstance(decoded, Mapping):
            _update_langfuse_generation(
                generation,
                output=decoded if full_langfuse_capture else None,
                metadata={**langfuse_metadata, "status": "invalid_schema"},
                usage_details=_openai_usage_details(response_payload),
            )
            return None
        _update_langfuse_generation(
            generation,
            output=dict(decoded) if full_langfuse_capture else None,
            metadata={**langfuse_metadata, "status": "success"},
            usage_details=_openai_usage_details(response_payload),
        )
        return decoded
    finally:
        _close_langfuse_generation(manager, sys.exc_info())


def _allowed_fields_for_tasks(tasks: list[str]) -> list[str]:
    if not tasks:
        return list(PLACE_LLM_REPAIR_FIELDS)
    allowed: list[str] = []
    if "dom_repair" in tasks:
        allowed.extend(PLACE_LLM_DOM_REPAIR_FIELDS)
    if "display_translation" in tasks:
        allowed.extend(PLACE_LLM_DISPLAY_TRANSLATION_FIELDS)
    if not allowed:
        return list(PLACE_LLM_REPAIR_FIELDS)
    return list(dict.fromkeys(allowed))


def _extract_chat_content(payload: Mapping[str, Any]) -> str | None:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, Mapping):
        return None
    message = first.get("message")
    if not isinstance(message, Mapping):
        return None
    content = message.get("content")
    return content if isinstance(content, str) and content.strip() else None


def _decode_json_object(content: str) -> object:
    stripped = content.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped, flags=re.DOTALL)
    if fenced is not None:
        stripped = fenced.group(1).strip()
    return json.loads(stripped)


def _resolve_llm_config_from_env(
    *,
    env_file: Path,
    default_config_file: Path,
    local_config_file: Path,
) -> _ResolvedLLMConfig:
    _load_env_file(env_file)
    settings = _merge_config(
        _FALLBACK_LLM_SETTINGS,
        _load_json_config(default_config_file),
    )
    settings = _merge_config(settings, _load_json_config(local_config_file))
    model_alias_or_id = os.environ.get("LLM_MODEL") or os.environ.get("OPENAI_MODEL")
    if not model_alias_or_id:
        raise LLMRepairError("LLM_MODEL or OPENAI_MODEL is required for LLM repair.")
    model_configs = _mapping(settings.get("models"), context="models")
    model_config = _mapping(model_configs.get(model_alias_or_id), context=model_alias_or_id)
    provider_name = (
        os.environ.get("LLM_PROVIDER")
        or _string(model_config.get("provider"))
        or "openai"
    )
    provider_configs = _mapping(settings.get("providers"), context="providers")
    provider_config = _mapping(provider_configs.get(provider_name), context=provider_name)
    if not provider_config:
        raise LLMRepairError(f"Unknown LLM provider: {provider_name}")
    model = _string(model_config.get("model")) or model_alias_or_id
    api_key = _resolve_api_key(provider_name, provider_config)
    base_url = _resolve_base_url(provider_name, provider_config)
    request_options = _resolve_request_options(settings, provider_config, model_config)
    return _ResolvedLLMConfig(
        api_key=api_key,
        base_url=base_url,
        model=model,
        request_options=request_options,
    )


def _resolve_api_key(provider_name: str, provider_config: Mapping[str, object]) -> str:
    api_key = os.environ.get("LLM_API_KEY")
    if api_key:
        return api_key
    api_key_env = _string(provider_config.get("api_key_env"))
    if api_key_env:
        api_key = os.environ.get(api_key_env)
        if api_key:
            return api_key
    if provider_name == "openai":
        raise LLMRepairError("OPENAI_API_KEY is required for LLM repair.")
    if api_key_env:
        raise LLMRepairError(f"{api_key_env} is required for LLM repair.")
    raise LLMRepairError(f"An API key is required for the {provider_name} LLM provider.")


def _resolve_base_url(provider_name: str, provider_config: Mapping[str, object]) -> str:
    base_url = os.environ.get("LLM_BASE_URL")
    if base_url:
        return base_url
    base_url_env = _string(provider_config.get("base_url_env"))
    if base_url_env:
        base_url = os.environ.get(base_url_env)
        if base_url:
            return base_url
    base_url = _string(provider_config.get("base_url"))
    if base_url:
        return base_url
    if provider_name == "openai":
        return _DEFAULT_OPENAI_BASE_URL
    raise LLMRepairError(f"A base URL is required for the {provider_name} LLM provider.")


def _resolve_request_options(
    settings: Mapping[str, object],
    provider_config: Mapping[str, object],
    model_config: Mapping[str, object],
) -> dict[str, object]:
    request_options: dict[str, object] = {}
    request_options.update(_mapping(settings.get("request_options"), context="request_options"))
    request_options.update(
        _mapping(provider_config.get("request_options"), context="provider.request_options")
    )
    request_options.update(
        _mapping(model_config.get("request_options"), context="model.request_options")
    )
    if "LLM_MAX_TOKENS" in os.environ:
        request_options["max_tokens"] = _parse_int_env("LLM_MAX_TOKENS")
    if "LLM_TEMPERATURE" in os.environ:
        request_options["temperature"] = _parse_float_env("LLM_TEMPERATURE")
    if "LLM_REASONING_EFFORT" in os.environ:
        request_options["reasoning_effort"] = os.environ["LLM_REASONING_EFFORT"]
    omit_temperature = _parse_bool_env("LLM_OMIT_TEMPERATURE")
    if omit_temperature is None:
        omit_temperature = _parse_bool(model_config.get("omit_temperature")) is True
    if omit_temperature:
        request_options.pop("temperature", None)
    return {
        key: value
        for key, value in request_options.items()
        if value is not None
    }


def _load_json_config(path: Path) -> Mapping[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LLMRepairError(f"Failed to read LLM config {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise LLMRepairError(f"LLM config {path} must contain a JSON object.")
    return payload


def _merge_config(base: Mapping[str, object], override: Mapping[str, object]) -> dict[str, object]:
    merged = dict(base)
    for key, override_value in override.items():
        base_value = merged.get(key)
        if isinstance(base_value, Mapping) and isinstance(override_value, Mapping):
            merged[key] = _merge_config(base_value, override_value)
            continue
        merged[key] = override_value
    return merged


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = _strip_env_value(value.strip())


def _strip_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _mapping(value: object, *, context: str) -> Mapping[str, object]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise LLMRepairError(f"LLM config field `{context}` must be a JSON object.")
    return value


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _parse_int_env(name: str) -> int:
    value = os.environ.get(name)
    if value is None:
        raise LLMRepairError(f"{name} is required.")
    try:
        return int(value)
    except ValueError as exc:
        raise LLMRepairError(f"{name} must be an integer.") from exc


def _parse_float_env(name: str) -> float:
    value = os.environ.get(name)
    if value is None:
        raise LLMRepairError(f"{name} is required.")
    try:
        return float(value)
    except ValueError as exc:
        raise LLMRepairError(f"{name} must be a number.") from exc


def _parse_bool_env(name: str) -> bool | None:
    value = os.environ.get(name)
    if value is None:
        return None
    return _parse_bool(value)


def _parse_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return None
