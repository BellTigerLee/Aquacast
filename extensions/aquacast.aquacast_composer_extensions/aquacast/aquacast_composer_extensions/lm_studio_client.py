"""Small local LLM HTTP client for Aquacast UI tools."""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class LMStudioClient:
    def __init__(
        self,
        base_url: str,
        *,
        model_name: str = "",
        timeout_s: float = 120.0,
        ollama_native: bool = False,
        keep_alive: str = "1h",
        num_ctx: int = 4096,
    ):
        self.base_url = str(base_url or "http://127.0.0.1:1234").rstrip("/")
        self.model_name = str(model_name or "").strip()
        self.timeout_s = float(timeout_s)
        self.ollama_native = bool(ollama_native)
        self.keep_alive = str(keep_alive or "1h")
        self.num_ctx = int(num_ctx or 4096)

    def first_model(self) -> str:
        if self.ollama_native:
            payload = self._get_json("/api/tags", timeout_s=min(30.0, self.timeout_s))
            models = payload.get("models", [])
            if not models:
                raise RuntimeError("Ollama returned no models. Pull a model first, for example gemma4.")
            model_id = models[0].get("name") or models[0].get("model")
            if not model_id:
                raise RuntimeError(f"Could not parse model id from Ollama response: {payload}")
            return str(model_id)

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
        if self.ollama_native:
            return self._ollama_generate(
                model_name,
                prompt,
                system_prompt=system_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
            )

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

    def _ollama_generate(
        self,
        model_name: str,
        prompt: str,
        *,
        system_prompt: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        payload = {
            "model": model_name,
            "system": str(system_prompt),
            "prompt": str(prompt),
            "stream": False,
            "think": False,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": float(temperature),
                "num_predict": int(max_tokens),
                "num_ctx": self.num_ctx,
            },
        }
        data = self._post_json("/api/generate", payload, timeout_s=self.timeout_s)
        try:
            return str(data.get("response") or "").strip()
        except Exception as exc:
            raise RuntimeError(f"Unexpected Ollama response: {data}") from exc

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
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
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
            raise RuntimeError(f"HTTP {exc.code} from local OpenAI-compatible LLM server: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Could not connect to local OpenAI-compatible LLM server at {self.base_url}."
            ) from exc
