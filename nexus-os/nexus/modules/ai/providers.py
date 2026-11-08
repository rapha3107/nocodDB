"""Interchangeable LLM providers.

Every provider implements `chat(messages, tools) -> {text, tool_calls, usage}` on a
provider-neutral message format:

    {"role": "system"|"user", "content": str}
    {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "arguments"}]}
    {"role": "tool", "tool_call_id": str, "name": str, "content": str}

and neutral tools `{"name": "games.launch", "description": str, "parameters": <JSON schema>}`.
Action names contain dots, which several APIs forbid in tool names: they are
sent as `games__launch` and mapped back.

Adding a provider = one subclass registered in PROVIDER_TYPES + one entry in
`ai_providers.json` (see config.py). All HTTP goes through `ctx.net` (HTTPS
allow-list, or http://127.0.0.1 for local servers).
"""

from __future__ import annotations

import json
import urllib.parse
import uuid

from ...core.errors import CommandFailed, NexusError, Unavailable
from ...core.net import LOCAL_HOSTS

PRO_NOTE = ("Rappel : un abonnement Claude Pro/Max (claude.ai) ne donne PAS accès à l'API ; "
            "il faut une clé créée sur console.anthropic.com, facturée à l'usage.")


def wire_name(action: str) -> str:
    return action.replace(".", "__")


def action_name(wire: str, known: dict[str, str] | None = None) -> str:
    if known and wire in known:
        return known[wire]
    return wire.replace("__", ".", 1)


class ProviderError(NexusError):
    """A provider call failed (network, timeout, HTTP error, bad answer): triggers local fallback."""

    code = "provider_error"
    http_status = 502

    def __init__(self, message: str, *, reason: str = "error", status: int | None = None, details=None):
        super().__init__(message, details=details)
        self.reason = reason
        self.status = status


def is_local_endpoint(endpoint: str) -> bool:
    host = (urllib.parse.urlparse(endpoint or "").hostname or "").lower()
    return host in LOCAL_HOSTS


class LLMProvider:
    type = "base"
    requires_key = False
    default_endpoint = ""
    default_timeout = 30.0

    def __init__(self, name: str, net, *, model: str | None, endpoint: str | None = None,
                 api_key: str | None = None, timeout: float | None = None):
        self.name = name
        self.net = net
        self.model = model
        self.endpoint = (endpoint or self.default_endpoint).rstrip("/")
        self.api_key = api_key
        self.timeout = timeout or self.default_timeout

    @property
    def remote(self) -> bool:
        return not is_local_endpoint(self.endpoint)

    def check_ready(self) -> None:
        if not self.model:
            raise Unavailable(f"Aucun modèle configuré pour le fournisseur « {self.name} ».")
        if self.requires_key and not self.api_key:
            raise Unavailable(self.missing_key_message())

    def missing_key_message(self) -> str:
        return f"Aucune clé API configurée pour « {self.name} » (Paramètres > NEXUS AI)."

    # subclasses: prepare() -> (url, payload, headers) ; parse(data, tool_map) -> result
    def prepare(self, messages: list[dict], tools: list[dict]) -> tuple[str, dict, dict]:
        raise NotImplementedError

    def parse(self, data: dict, tool_map: dict[str, str]) -> dict:
        raise NotImplementedError

    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        self.check_ready()
        url, payload, headers = self.prepare(messages, tools)
        tool_map = {wire_name(t["name"]): t["name"] for t in tools}
        try:
            data = self.net.post_json(url, payload, headers=headers, timeout=self.timeout)
        except NexusError as e:
            status = (e.details or {}).get("status")
            low = e.message.lower()
            reason = "timeout" if ("timeout" in low or "délai" in low) else "network"
            if isinstance(status, int):
                reason = {401: "unauthorized", 403: "forbidden", 429: "rate_limited"}.get(
                    status, "server_error" if status >= 500 else "http_error")
            msg = f"Le fournisseur « {self.name} » est injoignable ou a refusé la requête ({reason})."
            if self.type == "anthropic":
                msg += " " + PRO_NOTE
            raise ProviderError(msg, reason=reason, status=status) from e
        except ValueError as e:
            raise ProviderError(f"Réponse illisible du fournisseur « {self.name} ».", reason="bad_response") from e
        if not isinstance(data, dict):
            raise ProviderError(f"Réponse inattendue du fournisseur « {self.name} ».", reason="bad_response")
        if data.get("error") or data.get("type") == "error":
            err = data.get("error")
            etype = (err.get("type") if isinstance(err, dict) else None) or "api_error"
            reason = {"authentication_error": "unauthorized", "rate_limit_error": "rate_limited",
                      "overloaded_error": "server_error", "api_error": "server_error"}.get(etype, "api_error")
            msg = f"Le fournisseur « {self.name} » a renvoyé une erreur ({etype})."
            if self.type == "anthropic" and reason == "unauthorized":
                msg += " " + PRO_NOTE
            raise ProviderError(msg, reason=reason)
        try:
            out = self.parse(data, tool_map)
        except (KeyError, IndexError, TypeError, AttributeError) as e:
            raise ProviderError(f"Réponse inattendue du fournisseur « {self.name} ».", reason="bad_response") from e
        out.setdefault("usage", None)
        out["request_chars"] = len(json.dumps(payload, ensure_ascii=False))
        return out


def _new_id() -> str:
    return "call_" + uuid.uuid4().hex[:12]


def _args(v) -> dict:
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip():
        try:
            d = json.loads(v)
            return d if isinstance(d, dict) else {}
        except ValueError:
            return {}
    return {}


class LocalProvider(LLMProvider):
    """The deterministic router only (no model). Never does network I/O."""

    type = "local"

    @property
    def remote(self) -> bool:
        return False

    def chat(self, messages, tools):
        raise Unavailable("Le fournisseur « local » est le routeur déterministe : il n'a pas de modèle de langage.")


class OllamaProvider(LLMProvider):
    """Ollama /api/chat with `tools` (https://github.com/ollama/ollama/blob/main/docs/api.md)."""

    type = "ollama"
    default_endpoint = "http://127.0.0.1:11434"
    default_timeout = 60.0

    def prepare(self, messages, tools):
        msgs = []
        for m in messages:
            if m["role"] == "assistant":
                msgs.append({"role": "assistant", "content": m.get("content") or "",
                             **({"tool_calls": [{"function": {"name": wire_name(c["name"]), "arguments": c["arguments"]}}
                                                for c in m["tool_calls"]]} if m.get("tool_calls") else {})})
            elif m["role"] == "tool":
                msgs.append({"role": "tool", "content": m["content"], "tool_name": wire_name(m["name"])})
            else:
                msgs.append({"role": m["role"], "content": m["content"]})
        payload = {"model": self.model, "messages": msgs, "stream": False,
                   "options": {"temperature": 0.2}}
        if tools:
            payload["tools"] = [{"type": "function", "function": {"name": wire_name(t["name"]),
                                                                  "description": t["description"],
                                                                  "parameters": t["parameters"]}} for t in tools]
        return self.endpoint + "/api/chat", payload, {}

    def parse(self, data, tool_map):
        msg = data["message"]
        calls = [{"id": _new_id(), "name": action_name(c["function"]["name"], tool_map),
                  "arguments": _args(c["function"].get("arguments"))} for c in (msg.get("tool_calls") or [])]
        usage = None
        if "prompt_eval_count" in data or "eval_count" in data:
            usage = {"input_tokens": int(data.get("prompt_eval_count") or 0),
                     "output_tokens": int(data.get("eval_count") or 0)}
        return {"text": msg.get("content") or "", "tool_calls": calls, "usage": usage}


class AnthropicProvider(LLMProvider):
    """Anthropic Messages API (POST /v1/messages)."""

    type = "anthropic"
    requires_key = True
    default_endpoint = "https://api.anthropic.com"
    max_tokens = 1024

    def missing_key_message(self) -> str:
        return (f"Aucune clé API Anthropic configurée pour « {self.name} » (Paramètres > NEXUS AI). " + PRO_NOTE)

    def prepare(self, messages, tools):
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        out: list[dict] = []

        def push(role, blocks):
            if out and out[-1]["role"] == role:
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": role, "content": list(blocks)})

        for m in messages:
            if m["role"] == "user":
                push("user", [{"type": "text", "text": m["content"]}])
            elif m["role"] == "assistant":
                blocks = [{"type": "text", "text": m["content"]}] if m.get("content") else []
                blocks += [{"type": "tool_use", "id": c["id"], "name": wire_name(c["name"]), "input": c["arguments"]}
                           for c in m.get("tool_calls") or []]
                push("assistant", blocks)
            elif m["role"] == "tool":
                push("user", [{"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}])
        payload = {"model": self.model, "max_tokens": self.max_tokens, "messages": out}
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [{"name": wire_name(t["name"]), "description": t["description"],
                                 "input_schema": t["parameters"]} for t in tools]
        headers = {"x-api-key": self.api_key or "", "anthropic-version": "2023-06-01"}
        return self.endpoint + "/v1/messages", payload, headers

    def parse(self, data, tool_map):
        text, calls = [], []
        for b in data["content"]:
            if b.get("type") == "text":
                text.append(b.get("text", ""))
            elif b.get("type") == "tool_use":
                calls.append({"id": b["id"], "name": action_name(b["name"], tool_map), "arguments": _args(b.get("input"))})
        u = data.get("usage") or {}
        usage = ({"input_tokens": int(u.get("input_tokens") or 0), "output_tokens": int(u.get("output_tokens") or 0)}
                 if u else None)
        return {"text": "".join(text), "tool_calls": calls, "usage": usage}


class OpenAICompatProvider(LLMProvider):
    """OpenAI-compatible /v1/chat/completions (OpenAI, OpenRouter, Mistral, llama.cpp server, vLLM…)."""

    type = "openai_compat"
    default_endpoint = "https://api.openai.com"

    @property
    def requires_key(self) -> bool:  # local servers (llama.cpp, vLLM on 127.0.0.1) need no key
        return self.remote

    def prepare(self, messages, tools):
        msgs = []
        for m in messages:
            if m["role"] == "assistant":
                d = {"role": "assistant", "content": m.get("content") or None}
                if m.get("tool_calls"):
                    d["tool_calls"] = [{"id": c["id"], "type": "function",
                                        "function": {"name": wire_name(c["name"]),
                                                     "arguments": json.dumps(c["arguments"], ensure_ascii=False)}}
                                       for c in m["tool_calls"]]
                msgs.append(d)
            elif m["role"] == "tool":
                msgs.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
            else:
                msgs.append({"role": m["role"], "content": m["content"]})
        payload = {"model": self.model, "messages": msgs, "temperature": 0.2}
        if tools:
            payload["tools"] = [{"type": "function", "function": {"name": wire_name(t["name"]),
                                                                  "description": t["description"],
                                                                  "parameters": t["parameters"]}} for t in tools]
        url = self.endpoint + ("/chat/completions" if self.endpoint.endswith("/v1") else "/v1/chat/completions")
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        return url, payload, headers

    def parse(self, data, tool_map):
        msg = data["choices"][0]["message"]
        calls = [{"id": c.get("id") or _new_id(), "name": action_name(c["function"]["name"], tool_map),
                  "arguments": _args(c["function"].get("arguments"))} for c in (msg.get("tool_calls") or [])]
        u = data.get("usage") or {}
        usage = ({"input_tokens": int(u.get("prompt_tokens") or 0), "output_tokens": int(u.get("completion_tokens") or 0)}
                 if u else None)
        return {"text": msg.get("content") or "", "tool_calls": calls, "usage": usage}


PROVIDER_TYPES: dict[str, type[LLMProvider]] = {
    "local": LocalProvider,
    "ollama": OllamaProvider,
    "anthropic": AnthropicProvider,
    "openai_compat": OpenAICompatProvider,
}


def build(name: str, entry: dict, net, api_key: str | None, tier: str = "default") -> LLMProvider:
    cls = PROVIDER_TYPES.get(entry.get("type", name))
    if cls is None:
        raise Unavailable(f"Type de fournisseur inconnu : {entry.get('type')}")
    models = entry.get("models") or {}
    model = models.get(tier) or models.get("default") or models.get("strong") or models.get("economy")
    return cls(name, net, model=model, endpoint=entry.get("endpoint"), api_key=api_key,
               timeout=entry.get("timeout"))


def schema_for(params: dict) -> dict:
    """Registry.describe() params -> JSON schema."""
    types = {"str": "string", "int": "integer", "float": "number", "bool": "boolean", "list": "array",
             "dict": "object"}
    props, required = {}, []
    for k, p in params.items():
        s: dict = {}
        if p.get("type") in types:
            s["type"] = types[p["type"]]
        if p.get("description"):
            s["description"] = p["description"]
        if p.get("choices"):
            s["enum"] = p["choices"]
        props[k] = s
        if p.get("required"):
            required.append(k)
    out = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    return out


__all__ = ["LLMProvider", "ProviderError", "PROVIDER_TYPES", "build", "schema_for", "wire_name", "action_name",
           "PRO_NOTE", "CommandFailed", "is_local_endpoint"]
