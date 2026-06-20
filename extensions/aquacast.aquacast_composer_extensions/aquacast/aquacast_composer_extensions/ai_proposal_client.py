"""Dependency-free HTTP client for dashboard AI actuator proposals."""

from __future__ import annotations

import json
from urllib.parse import urlencode
import urllib.error
import urllib.request


class AIProposalClient:
    def __init__(self, base_url: str, *, timeout_s: float = 10.0):
        self.base_url = str(base_url or "http://127.0.0.1:8000").rstrip("/")
        self.timeout_s = float(timeout_s)

    def pending(self, *, limit: int = 20) -> dict:
        return self._get_json("/api/ai/actions/pending", {"limit": int(limit)})

    def recent(self, *, limit: int = 20) -> dict:
        return self._get_json("/api/ai/actions/recent", {"limit": int(limit)})

    def get(self, proposal_id: str) -> dict:
        return self._get_json(f"/api/ai/actions/{proposal_id}")

    def propose(self, *, tank_id: str | None = None, auto_alert: dict | None = None) -> dict:
        query = {"tank_id": str(tank_id)} if tank_id else None
        payload = {"auto_alert": auto_alert} if isinstance(auto_alert, dict) else {}
        return self._post_json("/api/ai/actions/propose", payload, query=query)

    def confirm(self, proposal_id: str, *, action_ids: list[str] | None = None, note: str = "Confirmed from Omniverse") -> dict:
        payload = {"operator": "omniverse", "note": note}
        if action_ids:
            payload["action_ids"] = list(action_ids)
        return self._post_json(f"/api/ai/actions/{proposal_id}/confirm", payload)

    def reject(self, proposal_id: str, *, note: str = "Rejected from Omniverse") -> dict:
        return self._post_json(f"/api/ai/actions/{proposal_id}/reject", {"operator": "omniverse", "note": note})

    def executions(self, proposal_id: str) -> dict:
        return self._get_json(f"/api/ai/actions/{proposal_id}/executions")

    def _get_json(self, path: str, query: dict | None = None) -> dict:
        suffix = f"?{urlencode(query)}" if query else ""
        request = urllib.request.Request(
            url=f"{self.base_url}{path}{suffix}",
            headers={"Accept": "application/json"},
            method="GET",
        )
        return self._open_json(request)

    def _post_json(self, path: str, payload: dict, query: dict | None = None) -> dict:
        suffix = f"?{urlencode(query)}" if query else ""
        request = urllib.request.Request(
            url=f"{self.base_url}{path}{suffix}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        return self._open_json(request)

    def _open_json(self, request: urllib.request.Request) -> dict:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code} from proposal backend: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not connect to proposal backend at {self.base_url}: {exc}") from exc
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Proposal backend returned non-JSON response: {raw}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"Proposal backend returned unexpected payload: {payload}")
        return payload
