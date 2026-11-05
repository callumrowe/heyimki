# Welcome to your Ki vault

This is an ordinary Obsidian vault. Open this folder in Obsidian, or point Ki
at a vault you already have by setting `KI_VAULT_PATH` in `.env`.

How Ki uses it:

- Ki searches every note when you ask about your projects, people or plans.
- `daily/YYYY-MM-DD.md` are daily notes. Ki creates one each midnight from
  `templates/daily.md`, and appends things you tell her under "Daily notes".
- Open todos (`- [ ] like this`) in daily notes are picked up by the heartbeat,
  which nudges you about them. Put a date in the todo — "Book dentist (2026-03-14)" —
  and Ki will bring it up closer to the day.
- New notes Ki writes go in `notes/`.

The folder layout is a default, not a rule: edit `api/prompts/instructions.md`
if you organise your vault differently.
