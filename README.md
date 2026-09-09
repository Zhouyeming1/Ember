<div align="center">

<img src="build/icon.png" width="96" alt="Ember logo" />

# Ember

**A local-first AI coding workbench**

Let DeepSeek and OpenAI-compatible models inspect, edit, and verify your repositories with explicit safety boundaries.

[English](README.md) · [简体中文](README.zh-CN.md) · [Download latest](https://github.com/Zhouyeming1/Ember/releases/latest)

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-macOS%20arm64%20%7C%20Windows%20x64-lightgrey)](https://github.com/Zhouyeming1/Ember/releases/latest)

</div>

Ember is an Electron desktop agent for real codebases. It brings model calls, workspace tools, terminal commands, permission prompts, session history, and diff review into one local workbench. The UI and session data stay on your machine; model requests go directly to the provider or local gateway you configure, with no cloud relay.

## Why Ember

- **DeepSeek first** — custom Base URL, model discovery, and reasoning-level controls, plus OpenAI-compatible endpoints such as OneAPI, Ollama, and vLLM.
- **Visible and controllable** — inspect tool calls, command output, file changes, and context usage as work happens.
- **Permission boundaries** — Plan, Ask, Workspace, and Full Access modes.
- **Recoverable edits** — patch checkpoints let `/undo` restore the previous turn's file changes.
- **Local-first state** — settings, credentials, and sessions live on your machine; no telemetry or cloud-hosted model proxy.
- **Desktop workflow** — project threads, `@` file mentions, steer-while-generating, image input, themes (white / paper / dark), diff previews, and Chinese/English UI.

## Architecture

Ember is self-contained — there is no third-party agent-runtime dependency. Everything that runs the agent lives in this repository:

```text
React Renderer
  conversation, diff, settings, project and session UI
        │  contextBridge / Electron IPC
        ▼
Electron Main
  windows, workspace, session index, agent host
  (local data layer: src/main/ember-core.ts)
        │  JSON-RPC over stdio  (python -m emberpy.rpc)
        ▼
emberpy — the Python agent engine (runtime-py/)
  agent loop · workspace tools · permission modes
  file patches & /undo checkpoints · hooks · skills · memory
```

- **The agent runs in its own Python worker** (`runtime-py/emberpy`, spawned as `python -m emberpy.rpc`). Permission modes, workspace tools, file patches, hooks, skills, memory, and JSONL session persistence are implemented there. The renderer only consumes typed events.
- **`src/main/ember-core.ts` is the Electron side's local data layer**: data home `~/.ember`, file-backed credentials (`auth.json`), settings, MCP config, and the session index (`state.sqlite`).
- The renderer has no direct Node.js access; every desktop capability crosses the typed IPC contract in `src/shared/types.ts`. After a crash, an on-disk session can be resumed as a conversation, but the engine never silently replays unfinished commands.

## Models and images

The desktop app currently focuses on DeepSeek and custom OpenAI-compatible Base URLs. API keys are stored locally in `~/.ember/auth.json` and are sent only to the provider or gateway you configure.

For pasted images:

- Vision can use official DeepSeek Vision, or a compatible endpoint such as GLM-4V.
- MinerU performs OCR by sending the image to the MinerU service; it is not offline local OCR.

## Permission modes

| Mode | Behaviour |
| --- | --- |
| `plan` | Read-only analysis and planning; diagnostic commands may run in a read-only sandbox |
| `ask` | Ask before writes, network access, or boundary escalation |
| `auto` | Run ordinary workspace operations automatically; ask on escalation |
| `full` | Disable workspace sandboxing for explicitly trusted projects |

Sandboxing is defense in depth, not a replacement for reviewing commands in an unfamiliar repository.

## Agent Skills

Skills are loaded by the Python engine. Standard locations:

| Scope | Path |
| --- | --- |
| Project (trusted) | `.agents/skills/<name>/SKILL.md` |
| User-global | `~/.ember/skills/<name>/SKILL.md`, `~/.agents/skills/<name>/SKILL.md` |

Each skill is a directory with a `SKILL.md` file. Frontmatter must include `name` and `description` (the engine validates; invalid skills are skipped).

- Invoke with `/skill:name`; type `/` in the composer to see loaded skills
- List paths and loaded skills under **Settings → Agent Skills**
- Project skills require trusting the workspace; `@` mentions only scan project `.agents/skills`

## Use Ember

Download from [GitHub Releases](https://github.com/Zhouyeming1/Ember/releases/latest):

- macOS: Apple Silicon / arm64
- Windows: Windows 10/11 x64

Then:

1. Open a project folder.
2. Configure a DeepSeek API key or compatible endpoint.
3. Describe a task, review tool activity and diffs, and use `/undo` when needed.

### Steer while generating

While a reply is generating, you can still type and press Enter. That text is steered into the current turn immediately (shown above the composer), not queued for later. Slash commands are not steered. Switching thread, starting a new chat, or changing project clears the on-screen steer list.

The current macOS package uses development signing. If Gatekeeper blocks it, right-click the app and choose **Open**, or run:

```bash
xattr -cr /Applications/Ember.app
```

## Develop locally

Requires Node.js `>=22.19`, pnpm, and a Python `>=3.11` that can import the engine.

```bash
git clone https://github.com/Zhouyeming1/Ember.git
cd Ember
pnpm install
pip install -e ./runtime-py   # make `emberpy` importable to the app's Python
pnpm dev
```

The agent worker runs as `python -m emberpy.rpc`. If your default `python` is not the one that has `emberpy` installed, point the `EMBERPY_PYTHON` environment variable at the right interpreter.

Checks:

```bash
pnpm typecheck
pnpm test
pnpm build
```

Python engine tests:

```bash
cd runtime-py && ./.venv/Scripts/python.exe -m pytest   # Windows
# or activate your runtime-py virtualenv first, then: pytest
```

## Privacy

Ember runs no telemetry or model relay service. Sessions, settings, and credentials stay local. To perform a task, prompts, relevant code context, and images are still sent to the model, gateway, or OCR service you choose. Review third-party privacy policies; sensitive projects can use a compatible local endpoint.

## License

[MIT](LICENSE).
