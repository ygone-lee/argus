# Contributing to Argus

Thanks for your interest! Issues and pull requests are welcome. 한국어·日本語로 써도 괜찮습니다.

## Reporting bugs and ideas

- Open a GitHub issue with what you did, what you expected, and what happened.
- Include your macOS version, Claude Code version (`claude --version`), Python version and browser.
- Server logs and screenshots help. **Remove tokens, Slack/Gmail content, file paths and conversation text you don't want public** before posting — Argus stores your Claude Code conversations, so logs can contain them.
- Security issues: please don't open a public issue. Use GitHub's private vulnerability reporting (Security → Report a vulnerability) instead.

## Development setup

1. Fork and clone the repository.
2. `./ctl.sh install` adds the hooks, the status line tap and the `argus-recall` skill (`./ctl.sh uninstall` removes them). It runs `install.py` with the system `/usr/bin/python3`.
3. `./ctl.sh start` (or `restart`) and open http://127.0.0.1:7799.

For UI work you can also run `./ctl.sh demo` (port 7798, made-up sessions, add `--loop` to replay the live part) — it never touches your own data, and it's a safe way to take screenshots for a PR.

Argus uses only the macOS system Python 3 (3.9+) standard library and plain HTML/CSS/JS — no package manager, build step or framework. Please keep it that way; a change that needs a new dependency should be discussed in an issue first.

## Adding a connector or a terminal

Both are plugins — one file each, no changes elsewhere needed for the basics. Start with [docs/connectors.md](docs/connectors.md) or [docs/terminals.md](docs/terminals.md); they list the interface, a complete example and how to test it. A PR adding one is very welcome — please say which service / terminal version you tested against.

## Pull requests

- **For anything bigger than a small fix, open an issue first** and describe what you want to change and why. That way we can agree on the approach before you spend time on it — a large PR that arrives unannounced may be closed if it doesn't fit the project's direction.
- Keep each PR focused on one change, and describe what it changes and how you checked it (which screen, which flow).
- Run the board and try the change in a real Claude Code session. For UI changes, include a screenshot or short recording.
- Update `README.md` when behavior, settings or files change.
- Code comments and UI text are written in Korean; the UI is translated by `csl/static/i18n.js`. When you add or change UI text, add the Japanese and English entries to the dictionary (untranslated strings show up in `window.__i18nMissing`).
- Never commit secrets or personal data. Connector secrets belong in the macOS Keychain, not in code, settings or logs.
- Follow the existing style: small functions, no new globals in `app.js` unless needed, standard library only on the server.

## Commit messages

One line summary, optionally prefixed with a type such as `feat:`, `fix:`, `docs:`, `style:` or `refactor:` (emoji like ✨ 🐛 💄 are fine).

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
