"""Optional Ollama assistant bridge, isolated from the main app API."""

from __future__ import annotations

import os
import re

from .ollama_support import DEFAULT_OLLAMA_URL, OllamaSupport

_MAX_LOG_CHARS = 12_000


def _redact_diagnostic_text(value: str) -> str:
    """Remove common credentials and personal identifiers before local AI analysis."""
    text = str(value)
    text = re.sub(
        r"(?i)(api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|passwd|secret|cookie|session(?:id)?|authorization)\s*[:=]\s*([^\s,;]+)",
        r"\1=[REDACTED]",
        text,
    )
    text = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
    text = re.sub(
        r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        "[EMAIL REDACTED]",
        text,
    )
    return text[-_MAX_LOG_CHARS:]


class OllamaSupportMixin:
    """Adds read-only Ollama helper endpoints without changing the task engine."""

    def open_ai_helper_window(self):
        """Open (or refocus + reload) the local Ollama support assistant."""
        import webview

        from ..config import GUI_DIR

        url = os.path.join(GUI_DIR, "ai_helper.html")
        current = getattr(self, "_ai_helper_window", None)
        if self._reuse_window(current, url):
            return

        self._ai_helper_window = webview.create_window(
            title="AutoRewarder AI Helper",
            url=url,
            js_api=self,
            width=820,
            height=680,
            min_size=(560, 480),
            resizable=True,
            background_color="#0d1117",
            text_select=True,
        )

    def list_ollama_models(self, base_url=DEFAULT_OLLAMA_URL):
        """List locally installed Ollama models for the read-only helper."""
        return OllamaSupport().list_models(base_url)

    def ask_ollama_support(
        self, question, model, base_url=DEFAULT_OLLAMA_URL,
        include_logs=False, history=None
    ):
        """Ask Ollama for read-only support; never changes app state or runs tasks."""
        logs = ""
        if include_logs:
            result = self.get_background_logs()
            if result.get("ok"):
                logs = _redact_diagnostic_text(result.get("content", ""))
        return OllamaSupport().chat(
            question=question,
            model=model,
            base_url=base_url,
            logs=logs,
            history=history,
        )
