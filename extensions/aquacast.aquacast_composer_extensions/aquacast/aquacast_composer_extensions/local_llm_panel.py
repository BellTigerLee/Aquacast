"""Local OpenAI-compatible LLM panel integrated into Aquacast."""

from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import urlencode
import urllib.request

import carb
import omni.kit.app
import omni.ui as ui

from .lm_studio_client import LMStudioClient
from .local_rag import build_rag_context


class LocalLLMPanel:
    def __init__(self, *, aquacast_main=None, config_getter=None):
        self._aquacast_main = aquacast_main
        self._config_getter = config_getter or (lambda _name, default=None: default)
        self._window = None
        self._running = False
        self._task = None
        self._rebuild_task = None
        self._messages = []
        self._server_url_model = None
        self._model_name_model = None
        self._interval_model = None
        self._prompt_model = None

    def show(self):
        if self._window is None:
            self._build_window()
        else:
            self._window.visible = True
        self._append_message_once(
            "System",
            "Local LLM Panel ready. It can use Ollama through http://127.0.0.1:1234 and local RAG context.",
        )

    def shutdown(self):
        self._stop_polling()
        if self._rebuild_task is not None:
            try:
                self._rebuild_task.cancel()
            except Exception:
                pass
            self._rebuild_task = None
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                pass
            self._window = None

    def _build_window(self):
        if self._server_url_model is None:
            self._server_url_model = ui.SimpleStringModel(
                self._config("LM_STUDIO_SERVER_URL", "http://127.0.0.1:1234")
            )
        if self._model_name_model is None:
            self._model_name_model = ui.SimpleStringModel(self._config("LM_STUDIO_MODEL_NAME", ""))
        if self._interval_model is None:
            self._interval_model = ui.SimpleIntModel(int(self._config("LM_STUDIO_POLL_INTERVAL_SECONDS", 60)))
        if self._prompt_model is None:
            self._prompt_model = ui.SimpleStringModel(
                self._config(
                    "LOCAL_LLM_DEFAULT_PROMPT",
                    self._config("LM_STUDIO_DEFAULT_PROMPT", "You are connected to Aquacast. Give a concise status-style response."),
                )
            )

        self._window = ui.Window("Aquacast Local LLM Panel", width=540, height=720, visible=True)
        self._build_window_contents()

    def _build_window_contents(self):
        if self._window is None:
            return
        with self._window.frame:
            with ui.VStack(spacing=8, height=0):
                ui.Label("Local LLM Connector", height=24)
                with ui.VStack(spacing=4):
                    ui.Label("Server URL. Ollama is exposed here for native and OpenAI-compatible APIs.")
                    ui.StringField(self._server_url_model)
                    ui.Label("Model name. Leave empty to auto-detect first loaded model.")
                    ui.StringField(self._model_name_model)
                    ui.Label("Prompt")
                    ui.StringField(self._prompt_model)
                    ui.Label("Interval seconds")
                    ui.IntField(self._interval_model)

                with ui.HStack(height=36, spacing=8):
                    ui.Button("Start", clicked_fn=self._start_polling)
                    ui.Button("Stop", clicked_fn=self._stop_polling)
                    ui.Button("Run Once", clicked_fn=self._run_once_clicked)
                    ui.Button("Clear", clicked_fn=self._clear_messages)

                ui.Separator()
                ui.Label("Latest Log", height=22)
                ui.Label(self._latest_log_text(), word_wrap=True, height=120)
                ui.Separator()
                ui.Label("Omniverse Console", height=22)
                ui.Label(
                    "Full Local LLM request/response history is written to the Omniverse Console. "
                    "Open Window > Utilities > Console and filter for [Aquacast Local LLM].",
                    word_wrap=True,
                    height=54,
                )

    def _start_polling(self):
        if self._running:
            self._append_message("System", "Polling is already running.")
            return
        self._running = True
        self._append_message("System", "Started polling local LLM.")
        self._task = self._schedule_task(self._poll_loop())
        if self._task is None:
            self._running = False

    def _stop_polling(self):
        self._running = False
        if self._task is not None:
            try:
                self._task.cancel()
            except Exception:
                pass
            self._task = None
        if self._window is not None:
            self._append_message("System", "Stopped polling.")

    def _run_once_clicked(self):
        self._schedule_task(self._run_once())

    def _clear_messages(self):
        self._messages.clear()
        self._request_ui_rebuild()

    async def _poll_loop(self):
        try:
            while self._running:
                await self._run_once()
                await asyncio.sleep(max(5, self._safe_int(self._interval_model, 60)))
        except asyncio.CancelledError:
            raise
        finally:
            self._running = False

    async def _run_once(self):
        prompt = self._model_string(self._prompt_model, "")
        server_url = self._model_string(self._server_url_model, "http://127.0.0.1:1234")
        self._append_message("API 요청", f"요청 URL: {server_url}\n프롬프트: {prompt}")
        try:
            response_text = await asyncio.to_thread(self._call_lm_studio, prompt)
            self._append_message("API 응답", f"요청 URL: {server_url}\n답변: {response_text}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._append_message("API연결 실패", f"요청 URL: {server_url}\n오류: {exc}")

    def _schedule_task(self, coro):
        try:
            task = asyncio.ensure_future(coro)
        except Exception as exc:
            try:
                coro.close()
            except Exception:
                pass
            self._append_message("API연결 실패", f"오류: {exc}")
            return None
        try:
            task.add_done_callback(self._on_task_done)
        except Exception:
            pass
        return task

    def _on_task_done(self, task):
        try:
            if task.cancelled():
                return
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self._append_message("API연결 실패", f"오류: {exc}")

    def _call_lm_studio(self, prompt: str) -> str:
        client = LMStudioClient(
            self._model_string(self._server_url_model, "http://127.0.0.1:1234"),
            model_name=self._model_string(self._model_name_model, self._config("LOCAL_LLM_MODEL_NAME", "")),
            timeout_s=float(self._config("LOCAL_LLM_TIMEOUT_SECONDS", self._config("LM_STUDIO_TIMEOUT_SECONDS", 120.0))),
            ollama_native=str(self._config("LOCAL_LLM_PROVIDER", "ollama")).strip().lower() == "ollama",
            keep_alive=str(self._config("LOCAL_LLM_KEEP_ALIVE", "1h")),
            num_ctx=int(self._config("LOCAL_LLM_NUM_CTX", 4096) or 4096),
        )
        prompt = self._prompt_with_rag(prompt)
        return client.chat(
            prompt,
            system_prompt=self._config(
                "LOCAL_LLM_SYSTEM_PROMPT",
                self._config(
                    "LM_STUDIO_SYSTEM_PROMPT",
                    "You are a concise Aquacast aquaculture assistant. Use provided RAG context when relevant.",
                ),
            ),
            temperature=float(self._config("LOCAL_LLM_TEMPERATURE", self._config("LM_STUDIO_TEMPERATURE", 0.7))),
            max_tokens=int(self._config("LOCAL_LLM_MAX_TOKENS", self._config("LM_STUDIO_MAX_TOKENS", 256))),
        )

    def _prompt_with_rag(self, prompt: str) -> str:
        context_parts = []
        if self._truthy(self._config("LOCAL_LLM_INCLUDE_WQ_DB_CONTEXT", True)):
            context_parts.append(self._water_quality_db_context())
        if not self._truthy(self._config("ENABLE_LOCAL_LLM_RAG", True)):
            context = "\n\n".join(part for part in context_parts if part)
            return f"{prompt}\n\n{context}" if context else prompt
        context_parts.append(
            build_rag_context(
                prompt,
                manuals_path=self._config("LOCAL_LLM_RAG_MANUALS_PATH", "~/cs-project/CSproject_Aqua/rag/manuals/documents.txt"),
                top_k=int(self._config("LOCAL_LLM_RAG_TOP_K", 3) or 3),
                max_chars=int(self._config("LOCAL_LLM_RAG_MAX_CHARS", 3500) or 3500),
            )
        )
        context = "\n\n".join(part for part in context_parts if part)
        return (
            f"{prompt}\n\n"
            "Use the following local SQLite/RAG context if it is relevant. "
            "If the context is insufficient, say what is missing instead of inventing data.\n\n"
            f"{context}"
        )

    def _water_quality_db_context(self) -> str:
        backend_url = str(self._config("WQ_BACKEND_URL", "http://127.0.0.1:8765")).rstrip("/")
        params = urlencode(
            {
                "hours": float(self._config("LOCAL_LLM_WQ_CONTEXT_HOURS", 4.0) or 4.0),
                "limit": int(self._config("LOCAL_LLM_WQ_CONTEXT_LIMIT", 7200) or 7200),
                "alert_limit": int(self._config("LOCAL_LLM_WQ_CONTEXT_ALERT_LIMIT", 200) or 200),
            }
        )
        url = f"{backend_url}/llm-context?{params}"
        max_chars = int(self._config("LOCAL_LLM_WQ_CONTEXT_MAX_CHARS", 5000) or 5000)
        try:
            request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
            with urllib.request.urlopen(request, timeout=5.0) as response:
                payload = json.loads(response.read().decode("utf-8") or "{}")
            text = str(payload.get("context_text") or payload)
            if len(text) > max_chars:
                text = text[:max_chars].rsplit(" ", 1)[0].strip()
            return text
        except Exception as exc:
            return f"[Aquacast SQLite water-quality context unavailable: {exc}]"

    def _append_message_once(self, role: str, text: str):
        if any(existing_role == role and existing_text == text for _ts, existing_role, existing_text in self._messages):
            return
        self._append_message(role, text)

    def _append_message(self, role: str, text: str):
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        entry = (timestamp, str(role), str(text))
        self._messages.append(entry)
        log_limit = int(self._config("LOCAL_LLM_RESPONSE_LOG_LIMIT", 0) or 0)
        if log_limit > 0:
            self._messages = self._messages[-log_limit:]
        self._log_to_omniverse_console(timestamp, role, text)
        self._request_ui_rebuild()

    @staticmethod
    def _log_to_omniverse_console(timestamp: str, role: str, text: str):
        message = f"[Aquacast Local LLM] [{timestamp}] {role}: {str(text).replace(chr(10), ' | ')}"
        if role == "API연결 실패":
            carb.log_error(message)
        else:
            # The built-in Console window hides Info by default, but shows Warning/Error.
            carb.log_warn(message)

    def _request_ui_rebuild(self):
        if self._window is None or self._rebuild_task is not None:
            return
        try:
            self._rebuild_task = asyncio.ensure_future(self._rebuild_on_next_update())
        except Exception as exc:
            self._rebuild_task = None
            carb.log_warn(f"[Aquacast Local LLM] Failed to schedule UI rebuild: {exc}")
            self._build_window_contents()

    async def _rebuild_on_next_update(self):
        try:
            try:
                await omni.kit.app.get_app().next_update_async()
            except Exception:
                pass
            self._build_window_contents()
        finally:
            self._rebuild_task = None

    def _latest_log_text(self) -> str:
        if not self._messages:
            return "No local LLM logs yet."
        timestamp, role, text = self._messages[-1]
        return f"[{timestamp}] {role}\n{text}"

    def _config(self, name: str, default=None):
        return self._config_getter(name, default)

    @staticmethod
    def _safe_int(model, default: int) -> int:
        if model is None:
            return int(default)
        try:
            return int(model.as_int)
        except Exception:
            pass
        try:
            return int(model.get_value_as_int())
        except Exception:
            return int(default)

    @staticmethod
    def _model_string(model, default: str = "") -> str:
        if model is None:
            return str(default)
        try:
            value = model.as_string
            if callable(value):
                value = value()
            return str(value)
        except Exception:
            pass
        try:
            return str(model.get_value_as_string())
        except Exception:
            return str(default)

    @staticmethod
    def _truthy(value) -> bool:
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
