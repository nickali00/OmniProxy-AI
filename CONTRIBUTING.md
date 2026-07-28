# Contributing

Thank you for helping improve OmniProxy AI.

## Development setup

```bash
git clone https://github.com/nickali00/OmniProxy-AI.git
cd OmniProxy-AI
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
```

For dashboard changes, also run:

```bash
node --check app/static/dashboard.js
node --check app/static/dashboard-i18n.js
```

## Pull requests

- Keep changes focused and explain their user-visible impact.
- Add or update tests for behavior changes.
- Preserve OpenAI-compatible response semantics.
- Never commit `.env`, API keys, provider sessions or copied user prompts.
- Update both README files when installation or public behavior changes.
- Keep Builder disabled by default unless the public security model changes.

Report security issues privately according to [SECURITY.md](SECURITY.md).
