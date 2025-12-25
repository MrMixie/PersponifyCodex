from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from typing import Iterable, List, Optional

from .base import BaseAdapter


class XAIAdapter(BaseAdapter):
    @property
    def type(self) -> str:
        return "xai"

    def is_available(self) -> bool:
        return bool(self._api_key())

    def capabilities(self) -> dict:
        return {
            "streaming": True,
            "system_prompt": True,
            "tool_calls": False,
            "resource": "remote",
        }

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
        if not api_key:
            raise RuntimeError("xAI API key missing")
        url = f"{self._base_url()}/models"
        data = self._get_json(url, api_key)
        models = data.get("data") or []
        return [{"id": item.get("id", "")} for item in models if item.get("id")]

    def _api_key(self) -> Optional[str]:
        # Environment variable keeps local testing simple.
        return self.settings.get("api_key") or os.environ.get("XAI_API_KEY")

    def _base_url(self) -> str:
        return str(self.settings.get("base_url") or "https://api.x.ai/v1").rstrip("/")

    def _model(self) -> str:
        return str(self.settings.get("model") or "grok-2-latest")

    def _timeout(self) -> float:
        return float(self.settings.get("timeout") or 60)

    def resource_hint(self) -> str:
        return "remote"

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
        if not api_key:
            raise RuntimeError("xAI API key missing")
        url = f"{self._base_url()}/chat/completions"
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=self._timeout()) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8") if exc.fp else str(exc)
            raise RuntimeError(f"xAI request failed: {exc.code} {detail}") from exc

    def _post_stream(self, payload: dict) -> Iterable[str]:
        api_key = self._api_key()
        if not api_key:
            raise RuntimeError("xAI API key missing")
        url = f"{self._base_url()}/chat/completions"
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
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
            raise RuntimeError(f"xAI request failed: {exc.code} {detail}") from exc

    def _get_json(self, url: str, api_key: str) -> dict:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=self._timeout()) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8") if exc.fp else str(exc)
            raise RuntimeError(f"xAI request failed: {exc.code} {detail}") from exc

    @staticmethod
    def _extract_text(data: dict) -> str:
        choices = data.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        content = message.get("content")
        if content is None:
            content = choices[0].get("text")
        return str(content or "")
