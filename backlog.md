## Ki builds itself — Phase 7 ✓ Built March 2026

Phase 7 is live. Ki can now:
- Edit `prompts/identity.md` and `prompts/instructions.md` directly (no confirmation)
- Write new integrations/tools as before
- Edit core Python files (`agent.py`, `main.py`, etc.) — but must call `request_confirmation` first

Remaining open questions for future expansion:
- Git diff visibility: currently Ki commits silently; could surface the diff before committing on core changes
- Rollback UX: `ki deploy` after `git revert HEAD` works but could be smoother

## Error handling ✓ Built March 2026

- Baileys: 5-minute timeout (covers browser tasks), retry x2 with backoff on connection errors, specific fallback messages (timeout vs connection failure)
- API: Qdrant down → note injected into Ki's system prompt so she knows vault answers may be incomplete
- Browser: screenshots sent to user on navigate errors and on every browser_act completion (last frame from history)
- All tool failures return "TOOL FAILED — {name}: {error}" so Ki is instructed to surface the exact error, never say "Done" on failure

## Security updates on the VPS ✓ Done

`apt install unattended-upgrades && dpkg-reconfigure --priority=low unattended-upgrades`
