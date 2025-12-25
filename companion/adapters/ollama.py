from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from typing import Iterable, List, Optional

from .base import BaseAdapter


class OllamaAdapter(BaseAdapter):
    @property
    def type(self) -> str:
        return "ollama"

    def is_available(self) -> bool:
        return self._probe_models()

    def capabilities(self) -> dict:
        return {
            "streaming": True,
            "system_prompt": True,
            "tool_calls": False,
            "resource": "local",
        }

    def complete(self, prompt: str, system: Optional[str] = None) -> str:
        url = f"{self._base_url()}/api/chat"
        payload = self._build_payload(prompt, system=system, stream=False)
        data = self._post_json(url, payload)
        message = (data.get("message") or {}).get("content")
        return str(message or "")

    def stream(self, prompt: str, system: Optional[str] = None) -> Iterable[str]:
        url = f"{self._base_url()}/api/chat"
        payload = self._build_payload(prompt, system=system, stream=True)
        for event in self._post_stream(url, payload):
            message = (event.get("message") or {}).get("content")
            if message:
                yield str(message)

    def list_models(self) -> List[dict]:
        url = f"{self._base_url()}/api/tags"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=self._timeout()) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8") if exc.fp else str(exc)
            raise RuntimeError(f"Ollama request failed: {exc.code} {detail}") from exc
        models = data.get("models") or []
        return [{"id": item.get("name", "")} for item in models if item.get("name")]

    def _base_url(self) -> str:
        # Default to the standard local Ollama port.
        return str(self.settings.get("base_url") or "http://127.0.0.1:11434").rstrip("/")

    def _model(self) -> str:
        return str(self.settings.get("model") or "llama3.1")

    def _timeout(self) -> float:
        return float(self.settings.get("timeout") or 60)

    def _availability_timeout(self) -> float:
        return float(self.settings.get("availability_timeout") or 2.0)

    def resource_hint(self) -> str:
        return "local"

    def _build_payload(self, prompt: str, system: Optional[str], stream: bool) -> dict:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": self._model(),
            "messages": messages,
            "stream": stream,
        }
        return payload

    def _post_json(self, url: str, payload: dict) -> dict:
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=self._timeout()) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8") if exc.fp else str(exc)
            raise RuntimeError(f"Ollama request failed: {exc.code} {detail}") from exc

    def _post_stream(self, url: str, payload: dict) -> Iterable[dict]:
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=self._timeout()) as resp:
                for raw in resp:
                    line = raw.decode("utf-8").strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    yield event
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8") if exc.fp else str(exc)
            raise RuntimeError(f"Ollama request failed: {exc.code} {detail}") from exc

    def _probe_models(self) -> bool:
        url = f"{self._base_url()}/api/tags"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(
                req,
                context=ssl.create_default_context(),
                timeout=self._availability_timeout(),
            ):
                return True
        except Exception:
            return False
