# automations

Personal automations. Each one is its own subfolder + its own GitHub Actions workflow, delivering results via [ntfy](https://ntfy.sh).

## Conventions

- One subfolder per automation, one workflow file per automation (`.github/workflows/<name>.yml`).
- Deterministic code only — no LLM calls at runtime. An earlier attempt at the movie digest used a Claude cloud agent session and it was unreliable (got stuck mid-run repeatedly); everything since is plain Python + APIs.
- Secrets via `gh secret set --repo shaanvaidya/automations`, never committed.
- Delivery via ntfy (topic per automation, or shared — check the workflow's `NTFY_TOPIC` secret).
- **After finishing or materially changing an automation, update `README.md`** with its status, schedule, what it does, and required secrets. Keep entries short.

See `README.md` for the current list of automations and their status.
