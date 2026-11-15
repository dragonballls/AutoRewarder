# Maintaining the AutoRewarder Ollama integration

The AI helper is kept in `src/assistant/` and `gui/ai_helper.html`. The main application API uses a small `OllamaSupportMixin` inheritance hook, while the main window has one Activity-panel button and one JavaScript opener. The existing search/task engine is not modified by the helper.

## Upstream update handling

The GitHub Actions workflow `.github/workflows/sync-upstream.yml` is also installed on the fork's default `main` branch, where GitHub can run its schedule. It runs daily and can also be started manually from the Actions tab. Keep the workflow on `main`, even though the maintained app code lives on `feat/local-ollama-support-assistant`. It fetches `safarsin/AutoRewarder:main` and merges those commits into `feat/local-ollama-support-assistant`.

A clean merge is syntax-checked and runs the Ollama unit tests before being pushed. If an upstream change conflicts with a local integration point, the workflow aborts the merge and fails rather than overwriting the AI helper. Resolve that conflict manually by preserving the upstream change and reapplying the small mixin/UI hook, then rerun the workflow.

## Limits

This process keeps the maintained branch current when upstream changes merge cleanly; it cannot guarantee that every future change is automatically compatible. Changes to the pywebview bridge, application startup, GUI navigation, packaging, or Python dependencies may require adapting the mixin and rebuilding. The helper intentionally uses only Ollama's loopback API and has no access to browser/task controls.
