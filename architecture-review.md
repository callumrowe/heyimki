# Ki Architecture Review

Five weaknesses identified in April 2026. Prioritised by impact on reliability and agent-editability.

---

## 1. Duplicate migration numbers + "skip on error" = invisible schema drift

**The problem:** Two different files are named `005_*` and two are named `006_*` in `api/migrations/`. More critically, `db.py:76` silently skips any migration that fails and starts the app anyway:

```python
except Exception as e:
    print(f"[migrations] ERROR: {version} failed and was skipped: {e}")
    failed.append(version)
```

When Ki writes a new migration with a bug in the SQL, the app starts with the wrong schema. The error only appears in startup logs — which nobody is watching. Every endpoint that touches the missing table or column then fails silently at runtime with a cryptic database error, not a clear "migration failed" message.

**The fix:** Change the migration runner to refuse to start if any migration fails. Add one line after the loop in `db.py`:

```python
if failed:
    raise RuntimeError(f"Migration failures — cannot start with broken schema: {failed}")
```

This converts silent runtime failures into loud startup failures, which are much easier to diagnose.

---

## 2. Port 8000 is exposed to the public internet, contradicting the docs

**The problem:** `CLAUDE.md` says `port 8000 now bound to 127.0.0.1 only`. The actual `docker-compose.yml` says:

```yaml
ports:
  - "8000:8000"   # exposed to ALL interfaces
```

Anyone who knows the VPS IP can reach the API directly. The `/message` endpoint has zero authentication — no secret, no JID check (that's only in Baileys). An attacker can POST to `http://<VPS-ip>:8000/message` and inject arbitrary messages. The `/heartbeat/trigger` endpoint is also unprotected.

**The fix:** One character change in `docker-compose.yml`:

```yaml
ports:
  - "127.0.0.1:8000:8000"
```

Traefik already handles all external traffic. Nothing legitimate needs port 8000 exposed directly to the internet.

---

## 3. The integration loader's `_SKIP` list is a fragile, easy-to-forget contract

**The problem:** When Ki writes a "library" Python file in `integrations/` (a pure module imported by other integrations, not itself an integration), she has to remember to manually add its name to the `_SKIP` set in `loader.py`. The April 2026 commit history shows the exact failure mode:

> `fix: skip google_calendar module — calendar tools now live in google.py, causing duplicate tool name error`

Forgetting to add to `_SKIP` crashed the stack on startup because two integrations tried to register tools with the same name. Ki won't naturally remember a rule buried in `loader.py` when she's focused on writing a new file.

**The fix:** Flip the convention — require library/helper files to signal themselves via their filename rather than requiring a central list to be updated. Rename helper files with a `_lib_` prefix and update the loader to skip anything starting with `_lib_`. The contract is then visible in the filename itself.

---

## 4. `respond()` and `respond_stream()` are 600 lines of duplicated code that regularly drift

**The problem:** `agent.py` has two nearly-identical code paths for WhatsApp (`respond()`) and desktop (`respond_stream()`). Both handle slash commands, pending action confirmation, session gap detection, vault search, cross-channel context, system prompt building, and image/document content building.

They've already drifted. RTF document handling exists only in `respond()` but is missing from `respond_stream()`. The session note text is longer and more detailed in `respond()` than in `respond_stream()`. When Ki fixes a bug on one path, she misses the other — reading 800 lines of code to find the parallel section is unreliable.

**The fix:** Extract a `_prepare_turn_context(user_message, channel, image, document)` helper that both methods call. It handles everything up to building `system` and `messages`. Each method then just runs the LLM loop in its own style (streaming vs. blocking). The shared logic becomes a single source of truth.

---

## 5. `agent.py` is an 1,800-line monolith — one syntax error kills the entire agent

**The problem:** `agent.py` contains everything: tool schema definitions (300 lines), a 200-line `_run_tool()` dispatcher, context building, history compression, correction detection, audio transcription, and two full response methods. There's even a duplicate constant:

```python
CODING_AGENT_URL = os.environ.get("CODING_AGENT_URL", ...)  # line 30
# ... 82 lines later ...
CODING_AGENT_URL = os.environ.get("CODING_AGENT_URL", ...)  # line 112 — duplicate
```

When Ki edits this file to add a tool or fix a bug, she's working next to completely unrelated code. A syntax error anywhere in the file — even in a section Ki didn't touch — brings the whole agent down. `main.py` imports `_INTEGRATIONS` and `_INTEGRATION_TOOL_MAP` directly from `agent.py`, so the entire startup depends on every line parsing correctly.

**The fix:** Split into logical modules:

- `agent/tools.py` — `_STATIC_TOOLS` definitions and `_run_tool()` dispatcher
- `agent/context.py` — `_build_system()`, `_compress_history()`, `_prepend_timestamp()`
- `agent/respond.py` — the `Agent` class with `respond()` and `respond_stream()`
- `agent/__init__.py` — re-exports for backwards compatibility

Ki editing the 200-line tools file no longer risks breaking the 600-line respond file. Each module fails loudly and independently.

---

## Impact summary

| # | Problem | Services break | Deploys break | Silent bug risk |
|---|---|:---:|:---:|:---:|
| 1 | Migration skip-on-error | ✓ | ✓ | High |
| 2 | Port 8000 exposed | ✓ | — | Medium |
| 3 | `_SKIP` list | ✓ | ✓ | High |
| 4 | Code duplication drift | ✓ | — | Medium |
| 5 | Monolith `agent.py` | ✓ | ✓ | High |

**Lowest effort, highest payoff:** #1 (migration fail-fast, ~3 lines) and #2 (port binding, 1 character). Both can be deployed in minutes and prevent entire categories of silent failures.
