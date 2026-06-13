import asyncio
import json
import time
import urllib.request
import urllib.error

import omni.ext
import omni.ui as ui
import omni.kit.async_engine

from configs import CONFIGS


class LocalLLMPanelExtension(omni.ext.IExt):
    """
    Minimal Omniverse extension that:
    1. Connects to LM Studio local OpenAI-compatible server.
    2. Sends a fixed prompt every N seconds.
    3. Prints the response into an Omniverse UI panel.
    """

    def on_startup(self, ext_id):
        self._ext_id = ext_id

        # config
        self._server_url_model = ui.SimpleStringModel(CONFIGS.get("server_url", "http://127.0.0.1:1234")) # local server url
        self._model_name_model = ui.SimpleStringModel(CONFIGS.get("model_name", "")) # 모델명
        self._interval_model = ui.SimpleIntModel(CONFIGS.get("interval", 60)) # interval
        self._prompt_model = ui.SimpleStringModel(CONFIGS.get("prompt", "You are connected to NVIDIA Omniverse. Give a concise status-style response.")) # prompt

        # Runtime state
        self._running = False
        self._task = None
        self._messages = []
        self._message_stack = None

        # UI
        self._window = ui.Window(
            "Local LLM Panel",
            width=520,
            height=720,
            visible=True
        )

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

        self._append_message("System", "Extension loaded. Start LM Studio server, then click Run Once or Start.")

    def on_shutdown(self):
        self._stop_polling()

        if self._window:
            self._window.destroy()
            self._window = None

    # -------------------------
    # UI event handlers
    # -------------------------

    def _start_polling(self):
        if self._running:
            self._append_message("System", "Polling is already running.")
            return

        self._running = True
        self._append_message("System", "Started polling LM Studio.")

        self._task = omni.kit.async_engine.run_coroutine(self._poll_loop())

    def _stop_polling(self):
        self._running = False

        if self._task:
            self._task.cancel()
            self._task = None

        self._append_message("System", "Stopped polling.")

    def _run_once_clicked(self):
        omni.kit.async_engine.run_coroutine(self._run_once())

    def _clear_messages(self):
        self._messages.clear()
        self._rebuild_messages()

    # -------------------------
    # Polling logic
    # -------------------------

    async def _poll_loop(self):
        while self._running:
            await self._run_once()

            interval = self._safe_int(self._interval_model.as_int, default=60)
            interval = max(5, interval)

            await asyncio.sleep(interval)

    async def _run_once(self):
        prompt = self._prompt_model.as_string
        self._append_message("Prompt", prompt)

        try:
            # Run blocking HTTP request off the main UI loop.
            response_text = await asyncio.to_thread(self._call_lm_studio, prompt)
            self._append_message("LLM", response_text)

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            self._append_message("Error", str(exc))

    # -------------------------
    # LM Studio API calls
    # -------------------------

    def _call_lm_studio(self, prompt: str) -> str:
        base_url = self._server_url_model.as_string.rstrip("/")
        model_name = self._model_name_model.as_string.strip()

        if not model_name:
            model_name = self._get_first_model(base_url)

        url = f"{base_url}/v1/chat/completions"

        payload = {
            "model": model_name,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a concise assistant running locally through LM Studio."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "temperature": 0.7,
            "max_tokens": 256,
            "stream": False
        }

        request = urllib.request.Request(
            url=url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json"
            },
            method="POST"
        )

        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = response.read().decode("utf-8")
                data = json.loads(raw)

        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code} from LM Studio: {body}") from exc

        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Could not connect to LM Studio at {base_url}. "
                f"Check that the LM Studio local server is running."
            ) from exc

        try:
            return data["choices"][0]["message"]["content"].strip()
        except Exception as exc:
            raise RuntimeError(f"Unexpected LM Studio response: {data}") from exc

    def _get_first_model(self, base_url: str) -> str:
        url = f"{base_url}/v1/models"

        request = urllib.request.Request(
            url=url,
            headers={"Content-Type": "application/json"},
            method="GET"
        )

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
                data = json.loads(raw)

        except Exception as exc:
            raise RuntimeError(
                "Could not auto-detect model from /v1/models. "
                "Either load a model in LM Studio or type the exact model name manually."
            ) from exc

        models = data.get("data", [])
        if not models:
            raise RuntimeError("LM Studio returned no models. Load Gemma in LM Studio first.")

        model_id = models[0].get("id")
        if not model_id:
            raise RuntimeError(f"Could not parse model id from LM Studio response: {data}")

        return model_id

    # -------------------------
    # UI rendering
    # -------------------------

    def _append_message(self, role: str, text: str):
        timestamp = time.strftime("%H:%M:%S")
        self._messages.append((timestamp, role, text))

        # Keep last 100 messages to avoid unlimited UI growth.
        self._messages = self._messages[-100:]

        self._rebuild_messages()

    def _rebuild_messages(self):
        if not self._message_stack:
            return

        self._message_stack.clear()

        with self._message_stack:
            for timestamp, role, text in self._messages:
                with ui.VStack(spacing=2):
                    ui.Label(f"[{timestamp}] {role}", height=20)
                    ui.Label(
                        text,
                        word_wrap=True,
                        height=0
                    )
                    ui.Separator()

    @staticmethod
    def _safe_int(fn, default: int) -> int:
        try:
            return int(fn())
        except Exception:
            return default