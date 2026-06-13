"""Local LM Studio panel integrated into the Aquacast Composer extension."""

from __future__ import annotations

import asyncio
import time

import omni.ui as ui

from .lm_studio_client import LMStudioClient


class LocalLLMPanel:
    def __init__(self, *, aquacast_main=None, config_getter=None):
        self._aquacast_main = aquacast_main
        self._config_getter = config_getter or (lambda _name, default=None: default)
        self._window = None
        self._running = False
        self._task = None
        self._messages = []
        self._message_stack = None
        self._server_url_model = None
        self._model_name_model = None
        self._interval_model = None
        self._prompt_model = None

    def show(self):
        if self._window is None:
            self._build_window()
        else:
            self._window.visible = True
        self._append_message_once("System", "Local LLM Panel ready. Start LM Studio, then click Run Once or Start.")

    def shutdown(self):
        self._stop_polling()
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                pass
            self._window = None
        self._message_stack = None

    def _build_window(self):
        self._server_url_model = ui.SimpleStringModel(
            self._config("LM_STUDIO_SERVER_URL", "http://127.0.0.1:1234")
        )
        self._model_name_model = ui.SimpleStringModel(self._config("LM_STUDIO_MODEL_NAME", ""))
        self._interval_model = ui.SimpleIntModel(int(self._config("LM_STUDIO_POLL_INTERVAL_SECONDS", 60)))
        self._prompt_model = ui.SimpleStringModel(
            self._config(
                "LM_STUDIO_DEFAULT_PROMPT",
                "You are connected to Aquacast. Give a concise status-style response.",
            )
        )

        self._window = ui.Window("Aquacast Local LLM Panel", width=540, height=720, visible=True)
        with self._window.frame:
            with ui.VStack(spacing=8, height=0):
                ui.Label("LM Studio Local LLM Connector", height=24)
                with ui.VStack(spacing=4):
                    ui.Label("LM Studio Server URL")
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
                ui.Label("Responses", height=22)
                with ui.ScrollingFrame(height=0):
                    self._message_stack = ui.VStack(spacing=6)

        self._rebuild_messages()

    def _start_polling(self):
        if self._running:
            self._append_message("System", "Polling is already running.")
            return
        self._running = True
        self._append_message("System", "Started polling LM Studio.")
        self._task = asyncio.ensure_future(self._poll_loop())

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
        asyncio.ensure_future(self._run_once())

    def _clear_messages(self):
        self._messages.clear()
        self._rebuild_messages()

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
        prompt = self._prompt_model.as_string if self._prompt_model is not None else ""
        self._append_message("Prompt", prompt)
        try:
            response_text = await asyncio.to_thread(self._call_lm_studio, prompt)
            self._append_message("LLM", response_text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._append_message("Error", str(exc))

    def _call_lm_studio(self, prompt: str) -> str:
        client = LMStudioClient(
            self._server_url_model.as_string if self._server_url_model is not None else "http://127.0.0.1:1234",
            model_name=self._model_name_model.as_string if self._model_name_model is not None else "",
            timeout_s=float(self._config("LM_STUDIO_TIMEOUT_SECONDS", 120.0)),
        )
        return client.chat(
            prompt,
            system_prompt=self._config(
                "LM_STUDIO_SYSTEM_PROMPT",
                "You are a concise assistant running locally through LM Studio for Aquacast.",
            ),
            temperature=float(self._config("LM_STUDIO_TEMPERATURE", 0.7)),
            max_tokens=int(self._config("LM_STUDIO_MAX_TOKENS", 256)),
        )

    def _append_message_once(self, role: str, text: str):
        if any(existing_role == role and existing_text == text for _ts, existing_role, existing_text in self._messages):
            return
        self._append_message(role, text)

    def _append_message(self, role: str, text: str):
        timestamp = time.strftime("%H:%M:%S")
        self._messages.append((timestamp, str(role), str(text)))
        self._messages = self._messages[-100:]
        self._rebuild_messages()

    def _rebuild_messages(self):
        if self._message_stack is None:
            return
        try:
            self._message_stack.clear()
        except Exception:
            return
        with self._message_stack:
            for timestamp, role, text in self._messages:
                with ui.VStack(spacing=2):
                    ui.Label(f"[{timestamp}] {role}", height=20)
                    ui.Label(text, word_wrap=True, height=0)
                    ui.Separator()

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
