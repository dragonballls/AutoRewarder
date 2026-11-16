# Maintaining the AutoRewarder Ollama integration

The read-only AI helper (`src/assistant/`, `gui/ai_helper.html`) has been removed. AutoRewarder's purpose is to complete Microsoft Rewards tasks automatically, and a chat assistant that only explains settings or summarizes logs earns no points; it is replaced by nothing, not by a smaller version of itself. The application API no longer inherits an `OllamaSupportMixin`, and the main window no longer shows an Activity-panel helper button.

What remains on this branch is the genuinely useful half of the Ollama work: Ollama as a **keyless local LLM provider for search-query generation**. That path makes the bot earn its points without a paid API key, so it stays.

## Upstream update handling

The GitHub Actions workflow `.github/workflows/sync-upstream.yml` is also installed on the fork's default `main` branch, where GitHub can run its schedule. It runs daily and can also be started manually from the Actions tab. Keep the workflow on `main`, even though the maintained app code lives on `feat/local-ollama-support-assistant`. It fetches `safarsin/AutoRewarder:main` and merges those commits into `feat/local-ollama-support-assistant`.

A clean merge is syntax-checked and runs the unit tests before being pushed. If an upstream change conflicts with a local integration point, the workflow aborts the merge and fails rather than overwriting local changes. Resolve that conflict manually by preserving the upstream change and reapplying the local change, then rerun the workflow.

## Limits

This process keeps the maintained branch current when upstream changes merge cleanly; it cannot guarantee that every future change is automatically compatible. Changes to the pywebview bridge, application startup, GUI navigation, packaging, or Python dependencies may require adapting the local Ollama query provider and rebuilding.
