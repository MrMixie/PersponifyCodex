from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from typing import Iterable, List, Optional
from urllib.parse import urlparse

from .base import BaseAdapter


class OpenAIAdapter(BaseAdapter):
    @property
    def type(self) -> str:
        return "openai"

    def is_available(self) -> bool:
        if self._requires_api_key():
            return bool(self._api_key())
        return self._probe_local_models()

    def capabilities(self) -> dict:
        return {
            "streaming": True,
            "system_prompt": True,
            "tool_calls": False,
            "resource": self.resource_hint(),
        }

    def resource_hint(self) -> str:
        return "local" if self._is_local_base_url() else "remote"

    def complete(self, prompt: str, system: Optional[str] = None) -> str:
        payload = self._build_payload(prompt, system=system, stream=False)
        data = self._post_json(payload)
        return self._extract_text(data)

    def stream(self, prompt: str, system: Optional[str] = None) -> Iterable[str]:
        payload = self._build_payload(prompt, system=system, stream=True)
        for chunk in self._post_stream(payload):
            yield chunk

    def list_models(self) -> List[dict]:
        api_key = self._api_key()
        if self._requires_api_key() and not api_key:
            raise RuntimeError("OpenAI API key missing")
        url = self._build_url(self._models_path())
        data = self._get_json(url, api_key)
        models = data.get("data") or []
        return [{"id": item.get("id", "")} for item in models if item.get("id")]

    def _api_key(self) -> Optional[str]:
        return self.settings.get("api_key") or os.environ.get("OPENAI_API_KEY")

    def _base_url(self) -> str:
        return str(self.settings.get("base_url") or "https://api.openai.com/v1").rstrip("/")

    def _models_path(self) -> str:
        return str(self.settings.get("models_path") or "/models")

    def _model(self) -> str:
        return str(self.settings.get("model") or "gpt-4o-mini")

    def _timeout(self) -> float:
        return float(self.settings.get("timeout") or 60)

    def _availability_timeout(self) -> float:
        return float(self.settings.get("availability_timeout") or 2.0)

    def _build_payload(self, prompt: str, system: Optional[str], stream: bool) -> dict:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {"model": self._model(), "messages": messages}
        temperature = self.settings.get("temperature")
        if temperature is not None:
            payload["temperature"] = temperature
        if stream:
            payload["stream"] = True
        return payload

    def _post_json(self, payload: dict) -> dict:
        api_key = self._api_key()
        if self._requires_api_key() and not api_key:
            raise RuntimeError("OpenAI API key missing")
        url = self._build_url("/chat/completions")
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        headers.update(self._extra_headers())
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=self._timeout()) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8") if exc.fp else str(exc)
            raise RuntimeError(f"OpenAI request failed: {exc.code} {detail}") from exc

    def _post_stream(self, payload: dict) -> Iterable[str]:
        api_key = self._api_key()
        if self._requires_api_key() and not api_key:
            raise RuntimeError("OpenAI API key missing")
        url = self._build_url("/chat/completions")
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        headers.update(self._extra_headers())
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=self._timeout()) as resp:
                for raw in resp:
                    line = raw.decode("utf-8").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    payload_str = line[5:].strip()
                    if payload_str == "[DONE]":
                        break
                    try:
                        data = json.loads(payload_str)
                    except json.JSONDecodeError:
                        continue
                    delta = (
                        data.get("choices", [{}])[0]
                        .get("delta", {})
                        .get("content")
                    )
                    if delta:
                        yield str(delta)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8") if exc.fp else str(exc)
            raise RuntimeError(f"OpenAI request failed: {exc.code} {detail}") from exc

    def _get_json(self, url: str, api_key: Optional[str]) -> dict:
        headers = {
            "Content-Type": "application/json",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        headers.update(self._extra_headers())
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=self._timeout()) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8") if exc.fp else str(exc)
            raise RuntimeError(f"OpenAI request failed: {exc.code} {detail}") from exc

    def _extra_headers(self) -> dict:
        extra = self.settings.get("headers") or {}
        if not isinstance(extra, dict):
            return {}
        return {str(k): str(v) for k, v in extra.items() if v is not None}

    def _probe_local_models(self) -> bool:
        url = self._build_url(self._models_path())
        headers = self._extra_headers()
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(
                req,
                context=ssl.create_default_context(),
                timeout=self._availability_timeout(),
            ):
                return True
        except Exception:
            return False

    def _requires_api_key(self) -> bool:
        # Local base URLs don't need a key unless explicitly required.
        setting = self.settings.get("require_api_key")
        if setting is not None:
            return bool(setting)
        return not self._is_local_base_url()

    def _is_local_base_url(self) -> bool:
        base = self._base_url()
        try:
            parsed = urlparse(base)
        except ValueError:
            return False
        host = (parsed.hostname or "").lower()
        if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}:
            return True
        if host.startswith("127."):
            return True
        return False

    def _build_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        base = self._base_url()
        if not path.startswith("/"):
            path = f"/{path}"
        return f"{base}{path}"

    @staticmethod
    def _extract_text(data: dict) -> str:
        choices = data.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text", ""))
            return "".join(parts)
        if content is None:
            content = choices[0].get("text")
        return str(content or "")
