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

from .ai_proposal_client import AIProposalClient
from .lm_studio_client import LMStudioClient
from .local_rag import build_rag_context


class LocalLLMPanel:
    _LOCAL_CONFIRM_SYNC_ACTIONS = {
        "set_temperature",
        "set_heater",
        "set_inlet_temperature",
        "set_inlet_temp",
        "set_water_exchange",
        "set_flow_rate",
        "set_inflow",
        "set_biofilter",
        "toggle_biofilter",
        "set_mechanical_filter",
        "set_solids_removal",
        "set_stock",
        "set_inlet_salinity",
        "set_salinity_in",
        "set_inlet_turbidity",
        "set_turbidity_in",
        "set_inlet_do",
        "set_inlet_alkalinity",
        "set_inlet_tan",
        "set_aeration",
        "set_kla_o2",
        "set_co2_stripping",
        "set_kla_co2",
        "set_biofilter_capacity",
        "set_nitrification_rate",
        "load_scenario",
    }
    _BEGINNER_DEFAULT_PROMPT = (
        "현재 Aquacast 수질 상태를 초보자용으로 설명하고, "
        "가장 먼저 확인할 항목 2가지를 알려줘."
    )
    _EXPERT_DEFAULT_PROMPT = (
        "Analyze the current Aquacast water-quality state using the latest SQLite/RAG context. "
        "Include threshold bands, trend deltas, likely cause, and control implications."
    )
    _BEGINNER_SYSTEM_INSTRUCTION = (
        "Operator profile: beginner. Answer in concise Korean unless the user asks otherwise. "
        "Explain operational meaning before technical detail, use plain terms for DO, TAN/NH3, CO2, pH, and turbidity, "
        "and finish with 1-3 safe next checks. Do not invent data and do not imply an actuator was applied unless confirmed."
    )
    _EXPERT_SYSTEM_INSTRUCTION = (
        "Operator profile: expert. Be concise but technical. Use the latest values, healthy/warn/critical bands, "
        "trend deltas, threshold alerts, actuator state, and RAG/SQLite source limitations when relevant. "
        "Call out missing evidence instead of guessing."
    )

    def __init__(self, *, aquacast_main=None, config_getter=None, post_confirm_callback=None):
        self._aquacast_main = aquacast_main
        self._config_getter = config_getter or (lambda _name, default=None: default)
        self._post_confirm_callback = post_confirm_callback
        self._window = None
        self._running = False
        self._task = None
        self._auto_alert_running = False
        self._auto_alert_task = None
        self._auto_alert_state = {}
        self._auto_alert_attempts = {}
        self._rebuild_task = None
        self._messages = []
        self._server_url_model = None
        self._model_name_model = None
        self._interval_model = None
        self._prompt_model = None
        self._proposal_backend_url_model = None
        self._proposals = []
        self._proposal_status = "No proposals loaded yet."

    def show(self):
        if self._window is None:
            self._build_window()
        else:
            self._window.visible = True
        self._append_message_once(
            "System",
            f"Local LLM Panel ready in {self._operator_level()} mode. It can use Ollama through http://127.0.0.1:1234 and local RAG context.",
        )
        self.start_auto_alert_monitor()

    def shutdown(self):
        self._stop_polling()
        self._stop_auto_alert_monitor()
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
            self._prompt_model = ui.SimpleStringModel(self._default_prompt())

        if self._proposal_backend_url_model is None:
            self._proposal_backend_url_model = ui.SimpleStringModel(
                self._config("AI_PROPOSAL_BACKEND_URL", "http://127.0.0.1:8000")
            )

        self._window = ui.Window("Aquacast Local LLM Panel", width=620, height=900, visible=True)
        self._build_window_contents()

    def _build_window_contents(self):
        if self._window is None:
            return
        with self._window.frame:
            with ui.ScrollingFrame():
                with ui.VStack(spacing=8, height=0):
                    ui.Label("Local LLM Connector", height=24)
                    with ui.VStack(spacing=4):
                        ui.Label("Server URL. Ollama is exposed here for native and OpenAI-compatible APIs.")
                        ui.StringField(self._server_url_model)
                        ui.Label("Model name. Leave empty to auto-detect first loaded model.")
                        ui.StringField(self._model_name_model)
                        ui.Label(f"Prompt ({self._operator_level()} mode)")
                        ui.StringField(self._prompt_model)
                        ui.Label("Interval seconds")
                        ui.IntField(self._interval_model)

                    with ui.HStack(height=36, spacing=8):
                        ui.Button("Start", clicked_fn=self._start_polling)
                        ui.Button("Stop", clicked_fn=self._stop_polling)
                        ui.Button("Run Once", clicked_fn=self._run_once_clicked)
                        ui.Button("Clear", clicked_fn=self._clear_messages)

                    ui.Separator()
                    self._build_proposal_inbox()
                    ui.Separator()
                    ui.Label("Latest Log", height=22)
                    with ui.ScrollingFrame(height=170):
                        ui.Label(self._latest_log_text(), word_wrap=True)
                    ui.Separator()
                    ui.Label("Omniverse Console", height=22)
                    ui.Label(
                        "Full Local LLM request/response history is written to the Omniverse Console. "
                        "Open Window > Utilities > Console and filter for [Aquacast Local LLM].",
                        word_wrap=True,
                        height=54,
                    )

    def _build_proposal_inbox(self):
        ui.Label("AI Proposal Inbox", height=24)
        ui.Label("Dashboard backend URL", height=20)
        ui.StringField(self._proposal_backend_url_model)
        with ui.HStack(height=34, spacing=8):
            ui.Button("Generate Proposal", clicked_fn=self._generate_proposal_clicked)
            ui.Button("Refresh Pending", clicked_fn=self._refresh_proposals_clicked)
            ui.Button("Refresh Recent", clicked_fn=self._refresh_recent_proposals_clicked)
        ui.Label(str(self._proposal_status), word_wrap=True, height=44)
        with ui.ScrollingFrame(height=230):
            with ui.VStack(spacing=8):
                if not self._proposals:
                    ui.Label("No proposals loaded. Click Refresh Pending or Generate Proposal.", word_wrap=True)
                for proposal in self._proposals:
                    self._build_proposal_row(proposal)

    def _build_proposal_row(self, proposal: dict):
        proposal_id = str(proposal.get("proposal_id") or "")
        status = str(proposal.get("status") or "unknown")
        risk = str(proposal.get("risk_level") or "watch")
        summary = str(proposal.get("summary") or "(no summary)")
        actions = proposal.get("actions") or []
        title = f"{risk.upper()} | {status} | {proposal_id[:8]}"
        with ui.VStack(spacing=4):
            ui.Label(title, height=20)
            ui.Label(self._proposal_target_text(proposal), word_wrap=True, height=24)
            ui.Label(self._proposal_evidence_text(proposal), word_wrap=True, height=44)
            ui.Label(summary, word_wrap=True, height=46)
            ui.Label(self._proposal_actions_text(actions), word_wrap=True, height=74)
            if proposal_id and status == "pending":
                with ui.HStack(height=30, spacing=8):
                    ui.Button("Confirm", clicked_fn=lambda pid=proposal_id: self._confirm_proposal_clicked(pid))
                    ui.Button("Reject", clicked_fn=lambda pid=proposal_id: self._reject_proposal_clicked(pid))

    def _start_polling(self):
        if self._running:
            self._append_message("System", "Polling is already running.")
            return
        self._running = True
        self._append_message("System", "Started polling local LLM.")
        self._task = self._schedule_task(self._poll_loop())
        if self._task is None:
            self._running = False

    def start_auto_alert_monitor(self):
        if not self._truthy(self._config("ENABLE_LOCAL_LLM_AUTO_ALERT_PROPOSALS", True)):
            return
        if self._auto_alert_running:
            return
        self._auto_alert_running = True
        self._auto_alert_task = self._schedule_task(self._auto_alert_loop())
        if self._auto_alert_task is None:
            self._auto_alert_running = False

    def _stop_auto_alert_monitor(self):
        self._auto_alert_running = False
        if self._auto_alert_task is not None:
            try:
                self._auto_alert_task.cancel()
            except Exception:
                pass
            self._auto_alert_task = None

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

    def _refresh_proposals_clicked(self):
        self._schedule_task(self._refresh_proposals(pending_only=True))

    def _refresh_recent_proposals_clicked(self):
        self._schedule_task(self._refresh_proposals(pending_only=False))

    def _generate_proposal_clicked(self):
        self._schedule_task(self._generate_proposal())

    def _confirm_proposal_clicked(self, proposal_id: str):
        self._schedule_task(self._confirm_proposal(proposal_id))

    def _reject_proposal_clicked(self, proposal_id: str):
        self._schedule_task(self._reject_proposal(proposal_id))

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

    async def _auto_alert_loop(self):
        try:
            while self._auto_alert_running:
                await self._auto_alert_check_once()
                interval = float(self._config("LOCAL_LLM_AUTO_ALERT_CHECK_INTERVAL_SECONDS", 10.0) or 10.0)
                await asyncio.sleep(max(2.0, interval))
        except asyncio.CancelledError:
            raise
        finally:
            self._auto_alert_running = False

    async def _auto_alert_check_once(self):
        try:
            alerts = await asyncio.to_thread(self._collect_auto_alerts)
        except Exception as exc:
            self._append_message("Auto Alert", f"Auto alert scan failed: {exc}")
            return
        limit = int(self._config("LOCAL_LLM_AUTO_ALERT_MAX_PROPOSALS_PER_CHECK", 1) or 1)
        for alert in alerts[: max(1, limit)]:
            await self._run_auto_alert_proposal(alert)

    async def _run_auto_alert_proposal(self, alert: dict):
        signature = str(alert.get("signature") or "")
        event_state_signature = str(alert.get("event_state_signature") or signature)
        state_key = str(alert.get("state_key") or "")
        self._auto_alert_attempts[event_state_signature] = time.monotonic()
        try:
            proposal = await asyncio.to_thread(
                self._proposal_client().propose,
                tank_id=alert.get("tank_path") or alert.get("tank_id"),
                auto_alert=alert,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._append_message("API Error", f"Auto alert proposal failed: {exc}")
            return

        if state_key and signature:
            self._auto_alert_state[state_key] = signature
        proposal_id = str(proposal.get("proposal_id") or "")
        if proposal_id:
            self._proposals = [item for item in self._proposals if str(item.get("proposal_id") or "") != proposal_id]
        self._proposals.insert(0, proposal)
        self._proposal_status = f"Auto alert proposal ready: {proposal_id[:8] or '(no id)'}"
        if self._window is None:
            self.show()
        else:
            self._window.visible = True
        self._append_message("Auto Alert", self._auto_alert_message(alert, proposal))
        self._request_ui_rebuild()

    async def _run_once(self):
        prompt = self._model_string(self._prompt_model, "")
        server_url = self._model_string(self._server_url_model, "http://127.0.0.1:1234")
        self._append_message("API Request", f"Request URL: {server_url}\nPrompt: {prompt}")
        try:
            response_text = await asyncio.to_thread(self._call_lm_studio, prompt)
            self._append_message("API Response", f"Request URL: {server_url}\nResponse: {response_text}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._append_message("API Error", f"Request URL: {server_url}\nError: {exc}")

    async def _refresh_proposals(self, *, pending_only: bool = True):
        label = "pending" if pending_only else "recent"
        self._proposal_status = f"Refreshing {label} proposals..."
        self._request_ui_rebuild()
        try:
            client = self._proposal_client()
            limit = int(self._config("AI_PROPOSAL_INBOX_LIMIT", 20) or 20)
            if pending_only:
                payload = await asyncio.to_thread(client.pending, limit=limit)
            else:
                payload = await asyncio.to_thread(client.recent, limit=limit)
            self._proposals = list(payload.get("proposals") or [])
            self._proposal_status = f"Loaded {len(self._proposals)} {label} proposal(s)."
            self._append_message("Proposal", self._proposal_status)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._proposal_status = f"Proposal refresh failed: {exc}"
            self._append_message("API Error", self._proposal_status)
        self._request_ui_rebuild()

    async def _generate_proposal(self):
        self._proposal_status = "Generating proposal from dashboard backend..."
        self._request_ui_rebuild()
        try:
            diagnostic = await asyncio.to_thread(self._proposal_context_diagnostic)
            self._append_message("Proposal", diagnostic)
            proposal = await asyncio.to_thread(self._proposal_client().propose)
            self._proposals = [proposal]
            proposal_id = str(proposal.get("proposal_id") or "")
            self._proposal_status = f"Generated proposal {proposal_id[:8]} with {len(proposal.get('actions') or [])} action(s)."
            self._append_message("Proposal", self._proposal_status)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._proposal_status = f"Proposal generation failed: {exc}"
            self._append_message("API Error", self._proposal_status)
        self._request_ui_rebuild()

    async def _confirm_proposal(self, proposal_id: str):
        self._proposal_status = f"Confirming proposal {proposal_id[:8]}..."
        self._request_ui_rebuild()
        try:
            result = await asyncio.to_thread(self._proposal_client().confirm, proposal_id)
            self._sync_local_confirmed_actions(result)
            self._proposal_status = f"Confirm result: {result.get('status')} for {proposal_id[:8]}"
            self._append_message("Proposal", f"{self._proposal_status}\n{json.dumps(result, ensure_ascii=False)}")
            await self._refresh_proposals(pending_only=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._proposal_status = f"Proposal confirm failed: {exc}"
            self._append_message("API Error", self._proposal_status)
            self._request_ui_rebuild()

    async def _reject_proposal(self, proposal_id: str):
        self._proposal_status = f"Rejecting proposal {proposal_id[:8]}..."
        self._request_ui_rebuild()
        try:
            result = await asyncio.to_thread(self._proposal_client().reject, proposal_id)
            self._proposal_status = f"Reject result: {result.get('status')} for {proposal_id[:8]}"
            self._append_message("Proposal", f"{self._proposal_status}\n{json.dumps(result, ensure_ascii=False)}")
            await self._refresh_proposals(pending_only=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._proposal_status = f"Proposal reject failed: {exc}"
            self._append_message("API Error", self._proposal_status)
            self._request_ui_rebuild()

    def _schedule_task(self, coro):
        try:
            task = asyncio.ensure_future(coro)
        except Exception as exc:
            try:
                coro.close()
            except Exception:
                pass
            self._append_message("API Error", f"Error: {exc}")
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
            self._append_message("API Error", f"Error: {exc}")

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
            system_prompt=self._llm_system_prompt(),
            temperature=float(self._config("LOCAL_LLM_TEMPERATURE", self._config("LM_STUDIO_TEMPERATURE", 0.7))),
            max_tokens=int(self._config("LOCAL_LLM_MAX_TOKENS", self._config("LM_STUDIO_MAX_TOKENS", 256))),
        )

    def _proposal_client(self) -> AIProposalClient:
        return AIProposalClient(
            self._model_string(self._proposal_backend_url_model, "http://127.0.0.1:8000"),
            timeout_s=float(self._config("AI_PROPOSAL_TIMEOUT_SECONDS", 60.0) or 60.0),
        )

    def _sync_local_confirmed_actions(self, confirm_result: dict):
        if self._aquacast_main is None:
            return
        if not hasattr(self._aquacast_main, "execute_water_quality_action"):
            self._append_message("Proposal", "Local Omniverse action API unavailable; confirm result was not locally synchronized.")
            return
        synced = 0
        skipped = []
        for execution in confirm_result.get("executions") or []:
            if str(execution.get("status") or "").lower() != "applied":
                continue
            payload = self._local_action_payload_from_execution(execution)
            if not payload:
                skipped.append(str(execution.get("action_type") or execution.get("id") or "unknown"))
                continue
            try:
                result = self._aquacast_main.execute_water_quality_action(payload)
            except Exception as exc:
                skipped.append(f"{payload.get('type', 'unknown')} error={exc}")
                continue
            if isinstance(result, dict) and result.get("status") == "ok":
                synced += 1
                self._append_message("Proposal", f"Synchronized local Omniverse action -> {json.dumps(payload, ensure_ascii=False)}")
                continue
            error = result.get("error", result.get("status", "unknown")) if isinstance(result, dict) else "unknown"
            skipped.append(f"{payload.get('type', 'unknown')} error={error}")
        if skipped:
            self._append_message("Proposal", f"Skipped local sync for {len(skipped)} execution(s): {', '.join(skipped[:5])}")
        if synced == 0 and not skipped:
            self._append_message("Proposal", "Confirm result contained no locally syncable applied actions.")
        if synced:
            self._run_post_confirm_callback(synced)

    def _run_post_confirm_callback(self, synced_count: int):
        callback = self._post_confirm_callback
        if not callable(callback):
            return
        try:
            callback()
            self._append_message("Proposal", f"Refreshed Omniverse UI after {int(synced_count)} synchronized action(s).")
        except Exception as exc:
            self._append_message("API Error", f"Post-confirm Omniverse refresh failed: {exc}")

    def _local_action_payload_from_execution(self, execution: dict) -> dict | None:
        source = execution.get("normalized_payload") or execution.get("request") or execution.get("payload") or {}
        if not isinstance(source, dict):
            return None
        kind = str(source.get("type") or source.get("action") or source.get("action_type") or execution.get("action_type") or "").strip().lower()
        if kind not in self._LOCAL_CONFIRM_SYNC_ACTIONS:
            return None

        params = source.get("params") or source.get("payload") or source.get("arguments") or {}
        payload = {"type": kind}
        if isinstance(params, dict):
            payload.update(params)
        for key, value in source.items():
            if key in {"type", "action", "action_type", "params", "payload", "arguments", "status", "operator", "note"}:
                continue
            payload.setdefault(key, value)

        tank_path = self._tank_path_from_action(source, execution)
        if tank_path:
            payload["tank_path"] = tank_path
        return payload

    def _tank_path_from_action(self, source: dict, execution: dict) -> str:
        for item in (source, execution):
            tank_path = str(item.get("tank_path") or "").strip()
            if tank_path:
                return tank_path
        tank_id = str(source.get("tank_id") or execution.get("tank_id") or "").strip()
        if not tank_id or self._aquacast_main is None or not hasattr(self._aquacast_main, "list_fish_tanks"):
            return ""
        try:
            for tank_path in self._aquacast_main.list_fish_tanks():
                if self._tank_id_from_path(str(tank_path)) == tank_id:
                    return str(tank_path)
        except Exception:
            return ""
        return ""

    @staticmethod
    def _tank_id_from_path(tank_path: str) -> str:
        parts = [part for part in str(tank_path).strip("/").split("/") if part]
        if parts and parts[-1] == "Water":
            parts = parts[:-1]
        generic = {"Root", "scene", "Meshes", "Model", "Components", "Component", "Water"}
        for part in reversed(parts):
            if part in generic:
                continue
            if part.startswith("Group") and part[5:].isdigit():
                continue
            return part
        return ""

    def _sync_local_inflow(self, enabled: bool):
        if self._aquacast_main is None or not hasattr(self._aquacast_main, "set_inflow"):
            return
        try:
            self._aquacast_main.set_inflow(bool(enabled))
            self._append_message("Proposal", f"Synchronized local Omniverse inflow state -> {'ON' if enabled else 'OFF'}")
        except Exception as exc:
            self._append_message("API Error", f"Local inflow synchronization failed: {exc}")

    @staticmethod
    def _bool_value(value):
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on", "enable", "enabled"}:
            return True
        if text in {"0", "false", "no", "off", "disable", "disabled"}:
            return False
        return None

    def _proposal_context_diagnostic(self) -> str:
        backend_url = str(self._config("WQ_BACKEND_URL", "http://127.0.0.1:8765")).rstrip("/")
        proposal_url = self._model_string(self._proposal_backend_url_model, "http://127.0.0.1:8000")
        params = urlencode(
            {
                "hours": float(self._config("LOCAL_LLM_WQ_CONTEXT_HOURS", 4.0) or 4.0),
                "limit": min(200, int(self._config("LOCAL_LLM_WQ_CONTEXT_LIMIT", 7200) or 7200)),
                "alert_limit": min(50, int(self._config("LOCAL_LLM_WQ_CONTEXT_ALERT_LIMIT", 200) or 200)),
            }
        )
        url = f"{backend_url}/llm-context?{params}"
        try:
            request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
            with urllib.request.urlopen(request, timeout=5.0) as response:
                payload = json.loads(response.read().decode("utf-8") or "{}")
            latest = payload.get("latest") or {}
            return (
                "Proposal context check: "
                f"WQ DB rows={payload.get('row_count', 0)}, "
                f"latest={latest.get('timestamp', latest.get('event_time_ms', 'none'))}, "
                f"alerts={payload.get('threshold_alert_count', 0)}. "
                f"Generate Proposal is delegated to {proposal_url}; SQL usage inside that backend is not verifiable from Omniverse."
            )
        except Exception as exc:
            return (
                "Proposal context check failed: "
                f"could not read {url}: {exc}. Generate Proposal is still delegated to {proposal_url}."
            )

    def _collect_auto_alerts(self) -> list[dict]:
        if self._aquacast_main is None:
            return []
        if not hasattr(self._aquacast_main, "list_fish_tanks") or not hasattr(self._aquacast_main, "get_quality_snapshot"):
            return []
        thresholds = self._metric_thresholds()
        if not thresholds:
            return []
        try:
            tank_paths = list(self._aquacast_main.list_fish_tanks() or [])
        except Exception:
            return []
        alerts = []
        for tank_path in tank_paths:
            try:
                snapshot = self._aquacast_main.get_quality_snapshot(tank_path=tank_path)
            except Exception:
                continue
            if not isinstance(snapshot, dict) or snapshot.get("status") != "ok":
                continue
            violations = self._threshold_violations(snapshot, thresholds)
            tank_id = self._tank_id_from_path(str(tank_path)) or str(snapshot.get("tank_id") or "")
            state_key = str(tank_id or tank_path)
            if not violations:
                self._auto_alert_state.pop(state_key, None)
                continue
            signature = self._auto_alert_signature(state_key, violations)
            event_state_signature = self._auto_alert_event_state_signature(state_key, violations)
            if self._auto_alert_state.get(state_key) == signature:
                continue
            if self._auto_alert_cooldown_suppressed(event_state_signature):
                continue
            severity = "critical" if any(item.get("band_state") == "critical" for item in violations) else "warning"
            alerts.append(
                {
                    "source": "aquacast_local_llm_panel",
                    "event_type": "auto_threshold_violation",
                    "severity": severity,
                    "state_key": state_key,
                    "signature": signature,
                    "event_state_signature": event_state_signature,
                    "tank_id": tank_id,
                    "tank_path": str(tank_path or snapshot.get("tank_path") or ""),
                    "violated_parameter_names": [item["parameter"] for item in violations],
                    "violations": violations,
                    "latest_sensor": self._auto_alert_sensor_snapshot(snapshot),
                    "operator_prompt": self._auto_alert_operator_prompt(tank_id or str(tank_path), severity, violations),
                }
            )
        return alerts

    def _metric_thresholds(self) -> dict:
        if self._aquacast_main is not None and hasattr(self._aquacast_main, "get_water_quality_metric_thresholds"):
            try:
                result = self._aquacast_main.get_water_quality_metric_thresholds()
                if isinstance(result, dict) and result.get("status") == "ok" and isinstance(result.get("thresholds"), dict):
                    return dict(result.get("thresholds") or {})
            except Exception:
                pass
        configured = self._config("WQ_METRIC_DASHBOARD_THRESHOLDS", {})
        return dict(configured or {}) if isinstance(configured, dict) else {}

    def _threshold_violations(self, snapshot: dict, thresholds: dict) -> list[dict]:
        violations = []
        for parameter, bands in sorted(thresholds.items()):
            if not isinstance(bands, dict) or parameter not in snapshot:
                continue
            value = self._float_or_none(snapshot.get(parameter))
            if value is None:
                continue
            state, condition = self._metric_band_state(value, bands)
            if state not in {"warn", "critical"}:
                continue
            violations.append(
                {
                    "parameter": str(parameter),
                    "label": self._metric_label(parameter),
                    "value": value,
                    "unit": self._metric_unit(parameter),
                    "band_state": state,
                    "threshold": dict(condition),
                    "condition": self._condition_label(condition),
                }
            )
        return violations

    def _metric_band_state(self, value: float, bands: dict) -> tuple[str, dict]:
        for state in ("critical", "warn", "healthy"):
            for condition in self._conditions_for_state(bands, state):
                if self._condition_matches(value, condition):
                    return state, condition
        return "unknown", {}

    @staticmethod
    def _conditions_for_state(bands: dict, state: str) -> list[dict]:
        raw = bands.get(state, []) if isinstance(bands, dict) else []
        if isinstance(raw, dict):
            raw = [raw]
        return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    @staticmethod
    def _condition_matches(value: float, condition: dict) -> bool:
        checks = (
            ("lt", lambda actual, limit: actual < limit),
            ("lte", lambda actual, limit: actual <= limit),
            ("gt", lambda actual, limit: actual > limit),
            ("gte", lambda actual, limit: actual >= limit),
        )
        for key, predicate in checks:
            limit = LocalLLMPanel._float_or_none(condition.get(key))
            if limit is not None and not predicate(value, limit):
                return False
        return True

    @staticmethod
    def _condition_label(condition: dict) -> str:
        lower_key = "gte" if "gte" in condition else "gt" if "gt" in condition else ""
        upper_key = "lte" if "lte" in condition else "lt" if "lt" in condition else ""
        if lower_key and upper_key:
            lower_op = ">=" if lower_key == "gte" else ">"
            upper_op = "<=" if upper_key == "lte" else "<"
            return f"{lower_op}{condition[lower_key]:g} and {upper_op}{condition[upper_key]:g}"
        if lower_key:
            lower_op = ">=" if lower_key == "gte" else ">"
            return f"{lower_op}{condition[lower_key]:g}"
        if upper_key:
            upper_op = "<=" if upper_key == "lte" else "<"
            return f"{upper_op}{condition[upper_key]:g}"
        return "any"

    @staticmethod
    def _auto_alert_signature(state_key: str, violations: list[dict]) -> str:
        parts = [
            f"{item.get('parameter')}:{item.get('band_state')}:{item.get('condition')}"
            for item in violations
        ]
        return f"{state_key}|" + ",".join(sorted(parts))

    @staticmethod
    def _auto_alert_event_state_signature(state_key: str, violations: list[dict]) -> str:
        parts = [
            f"{item.get('parameter')}:{item.get('band_state')}"
            for item in violations
        ]
        return f"{state_key}|" + ",".join(sorted(parts))

    def _auto_alert_cooldown_suppressed(self, event_state_signature: str) -> bool:
        previous = self._auto_alert_attempts.get(event_state_signature)
        if previous is None:
            return False
        cooldown_s = float(
            self._config(
                "LOCAL_LLM_AUTO_ALERT_SAME_EVENT_COOLDOWN_SECONDS",
                self._config("LOCAL_LLM_AUTO_ALERT_RETRY_SECONDS", 60.0),
            )
            or 60.0
        )
        return time.monotonic() - float(previous) < max(1.0, cooldown_s)

    @staticmethod
    def _auto_alert_sensor_snapshot(snapshot: dict) -> dict:
        keys = (
            "temperature_c",
            "dissolved_oxygen_mg_l",
            "tan_mg_l",
            "nh3_mg_l",
            "ph",
            "co2_mg_l",
            "alkalinity_mg_l_as_caco3",
            "salinity_ppt",
            "turbidity_ntu",
            "nitrite_mg_l",
            "nitrate_mg_l",
            "inflow_enabled",
            "biofilter_on",
            "mechanical_filter_on",
            "heater_power_w",
            "flow_lph",
            "q_makeup_lph",
            "inlet_temp_c",
        )
        return {key: snapshot.get(key) for key in keys if key in snapshot}

    @staticmethod
    def _metric_label(parameter: str) -> str:
        labels = {
            "temperature_c": "temperature",
            "dissolved_oxygen_mg_l": "dissolved oxygen",
            "tan_mg_l": "total ammonia nitrogen",
            "nh3_mg_l": "unionized ammonia",
            "ph": "pH",
            "co2_mg_l": "carbon dioxide",
            "alkalinity_mg_l_as_caco3": "alkalinity",
            "salinity_ppt": "salinity",
            "turbidity_ntu": "turbidity",
            "nitrite_mg_l": "nitrite",
            "nitrate_mg_l": "nitrate",
        }
        return labels.get(str(parameter), str(parameter))

    @staticmethod
    def _metric_unit(parameter: str) -> str:
        units = {
            "temperature_c": "C",
            "dissolved_oxygen_mg_l": "mg/L",
            "tan_mg_l": "mg/L",
            "nh3_mg_l": "mg/L",
            "co2_mg_l": "mg/L",
            "alkalinity_mg_l_as_caco3": "mg/L as CaCO3",
            "salinity_ppt": "ppt",
            "turbidity_ntu": "NTU",
            "nitrite_mg_l": "mg/L",
            "nitrate_mg_l": "mg/L",
        }
        return units.get(str(parameter), "")

    def _auto_alert_operator_prompt(self, tank: str, severity: str, violations: list[dict]) -> str:
        reasons = "; ".join(
            f"{item.get('label')}={self._short_value(item.get('value'))}{item.get('unit', '')} is {item.get('band_state')} because condition is {item.get('condition')}"
            for item in violations
        )
        return (
            f"Tank {tank} is currently {severity}. Explain why this state is {severity} using the measured value and threshold condition, "
            f"then propose safe corrective actions that require operator confirmation. Evidence: {reasons}."
        )

    def _auto_alert_message(self, alert: dict, proposal: dict) -> str:
        proposal_id = str(proposal.get("proposal_id") or "")
        reason = "; ".join(
            f"{item.get('parameter')}={self._short_value(item.get('value'))} {item.get('band_state')} ({item.get('condition')})"
            for item in alert.get("violations") or []
        )
        return f"{str(alert.get('severity')).upper()} auto alert created proposal {proposal_id[:8]} for {alert.get('tank_id') or alert.get('tank_path')}: {reason}"

    @staticmethod
    def _float_or_none(value) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _prompt_with_rag(self, prompt: str) -> str:
        context_parts = []
        profile_instruction = self._profile_context_instruction()
        if self._truthy(self._config("LOCAL_LLM_INCLUDE_WQ_DB_CONTEXT", True)):
            context_parts.append(self._water_quality_db_context())
        if not self._truthy(self._config("ENABLE_LOCAL_LLM_RAG", True)):
            context = "\n\n".join(part for part in context_parts if part)
            return f"{prompt}\n\n{profile_instruction}\n\n{context}" if context else f"{prompt}\n\n{profile_instruction}"
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
            f"{profile_instruction}\n\n"
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
        default_max_chars = 2500 if self._operator_level() == "beginner" else 5000
        max_chars = int(self._config("LOCAL_LLM_WQ_CONTEXT_MAX_CHARS", default_max_chars) or default_max_chars)
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
        if role == "API Error":
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
        try:
            display_limit = int(self._config("LOCAL_LLM_PANEL_DISPLAY_LOG_LIMIT", 100))
        except (TypeError, ValueError):
            display_limit = 100
        messages = self._messages[-display_limit:] if display_limit > 0 else self._messages
        return "\n\n".join(f"[{timestamp}] {role}\n{text}" for timestamp, role, text in messages)

    @staticmethod
    def _proposal_actions_text(actions) -> str:
        if not actions:
            return "Actions: none"
        lines = []
        for index, action in enumerate(actions[:5], start=1):
            action_type = action.get("action_type") or action.get("type") or "unknown"
            params = action.get("params") or action.get("payload") or {}
            target = LocalLLMPanel._action_target_text(action)
            rationale = action.get("rationale") or ""
            lines.append(f"{index}. {action_type} {target} {params} {rationale}".strip())
        if len(actions) > 5:
            lines.append(f"... {len(actions) - 5} more")
        return "\n".join(lines)

    @staticmethod
    def _proposal_target_text(proposal: dict) -> str:
        latest = proposal.get("latest_sensor") if isinstance(proposal.get("latest_sensor"), dict) else {}
        tank_id = str(proposal.get("target_tank_id") or latest.get("tank_id") or "").strip()
        tank_path = str(proposal.get("target_tank_path") or latest.get("tank_path") or "").strip()
        if not tank_id and tank_path:
            tank_id = LocalLLMPanel._tank_id_from_path(tank_path)
        if tank_id or tank_path:
            return f"Target tank: {tank_id or '(unknown)'} | {tank_path or '(no path)'}"
        return "Target tank: not specified"

    def _proposal_evidence_text(self, proposal: dict) -> str:
        evidence = proposal.get("context_evidence") if isinstance(proposal.get("context_evidence"), dict) else {}
        latest = evidence.get("latest_sensor") if isinstance(evidence.get("latest_sensor"), dict) else {}
        thresholds = evidence.get("threshold_reference") if isinstance(evidence.get("threshold_reference"), dict) else {}
        rag = proposal.get("rag_status") if isinstance(proposal.get("rag_status"), dict) else {}
        expert = self._operator_level() == "expert"
        temp = latest.get("temperature_c", latest.get("temperature"))
        do_value = latest.get("DO", latest.get("dissolved_oxygen_mg_l"))
        tan = latest.get("TAN", latest.get("tan_mg_l"))
        nh3 = latest.get("ammonia", latest.get("nh3_mg_l"))
        ph = latest.get("pH", latest.get("ph"))
        temp_threshold = thresholds.get("temperature_c") if isinstance(thresholds.get("temperature_c"), dict) else {}
        rag_mode = str(rag.get("mode") or "unknown")
        rag_note = "fallback" if rag.get("fallback") else "active"
        parts = []
        if temp is not None:
            parts.append(f"T={LocalLLMPanel._short_value(temp)}C")
        if do_value is not None:
            parts.append(f"DO={LocalLLMPanel._short_value(do_value)}")
        if ph is not None:
            parts.append(f"pH={LocalLLMPanel._short_value(ph)}")
        if expert and tan is not None:
            parts.append(f"TAN={LocalLLMPanel._short_value(tan)}")
        if nh3 is not None:
            parts.append(f"NH3={LocalLLMPanel._short_value(nh3)}")
        if expert and temp_threshold:
            parts.append(f"temp bands={temp_threshold}")
        parts.append(f"RAG={rag_mode}/{rag_note}" if expert else f"mode={self._operator_level()}")
        return "Evidence: " + " | ".join(str(part) for part in parts)

    @staticmethod
    def _short_value(value) -> str:
        try:
            return f"{float(value):.2f}"
        except Exception:
            return str(value)

    @staticmethod
    def _action_target_text(action: dict) -> str:
        payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
        params = action.get("params") if isinstance(action.get("params"), dict) else {}
        tank_id = str(action.get("tank_id") or payload.get("tank_id") or params.get("tank_id") or "").strip()
        tank_path = str(action.get("tank_path") or payload.get("tank_path") or params.get("tank_path") or "").strip()
        if not tank_id and tank_path:
            tank_id = LocalLLMPanel._tank_id_from_path(tank_path)
        if tank_id or tank_path:
            return f"target={tank_id or tank_path}"
        return "target=unspecified"

    def _config(self, name: str, default=None):
        return self._config_getter(name, default)

    def _operator_level(self) -> str:
        raw = self._config("AQUACAST_OPERATOR_LEVEL", self._config("OPERATOR_LEVEL", "beginner"))
        return "expert" if str(raw or "").strip().lower() == "expert" else "beginner"

    def _default_prompt(self) -> str:
        configured = self._config_text("LOCAL_LLM_DEFAULT_PROMPT")
        if configured:
            return configured
        return self._EXPERT_DEFAULT_PROMPT if self._operator_level() == "expert" else self._BEGINNER_DEFAULT_PROMPT

    def _llm_system_prompt(self) -> str:
        base = self._config_text("LOCAL_LLM_SYSTEM_PROMPT") or self._config_text("LM_STUDIO_SYSTEM_PROMPT")
        if not base:
            base = "You are a concise Aquacast aquaculture assistant. Use provided RAG context when relevant."
        return f"{base}\n\n{self._profile_context_instruction()}"

    def _profile_context_instruction(self) -> str:
        if self._operator_level() == "expert":
            return self._EXPERT_SYSTEM_INSTRUCTION
        return self._BEGINNER_SYSTEM_INSTRUCTION

    def _config_text(self, name: str) -> str:
        value = self._config(name, "")
        return str(value or "").strip()

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
