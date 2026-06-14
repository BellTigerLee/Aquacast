"""Small LM Studio OpenAI-compatible HTTP client for Aquacast UI tools."""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class LMStudioClient:
    def __init__(self, base_url: str, *, model_name: str = "", timeout_s: float = 120.0):
        self.base_url = str(base_url or "http://127.0.0.1:1234").rstrip("/")
        self.model_name = str(model_name or "").strip()
        self.timeout_s = float(timeout_s)

    def first_model(self) -> str:
        payload = self._get_json("/v1/models", timeout_s=min(30.0, self.timeout_s))
        models = payload.get("data", [])
        if not models:
            raise RuntimeError("LM Studio returned no models. Load a model in LM Studio first.")
        model_id = models[0].get("id")
        if not model_id:
            raise RuntimeError(f"Could not parse model id from LM Studio response: {payload}")
        return str(model_id)

    def chat(
        self,
        prompt: str,
        *,
        system_prompt: str = "You are a concise assistant running locally through LM Studio.",
        temperature: float = 0.7,
        max_tokens: int = 256,
    ) -> str:
        model_name = self.model_name or self.first_model()
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": str(system_prompt)},
                {"role": "user", "content": str(prompt)},
            ],
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
            "stream": False,
        }
        data = self._post_json("/v1/chat/completions", payload, timeout_s=self.timeout_s)
        try:
            return str(data["choices"][0]["message"]["content"]).strip()
        except Exception as exc:
            raise RuntimeError(f"Unexpected LM Studio response: {data}") from exc

    def _get_json(self, path: str, *, timeout_s: float) -> dict:
        request = urllib.request.Request(
            url=f"{self.base_url}{path}",
            headers={"Content-Type": "application/json"},
            method="GET",
        )
        return self._open_json(request, timeout_s=timeout_s)

    def _post_json(self, path: str, payload: dict, *, timeout_s: float) -> dict:
        request = urllib.request.Request(
            url=f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self._open_json(request, timeout_s=timeout_s)

    def _open_json(self, request: urllib.request.Request, *, timeout_s: float) -> dict:
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw or "{}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code} from LM Studio: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Could not connect to LM Studio at {self.base_url}. Check that the LM Studio local server is running."
            ) from exc
