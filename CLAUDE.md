# CLAUDE.md — arXiv Digest

Project-specific conventions for AI-assisted development.
Global rules live in `~/.claude/CLAUDE.md`.

---

## Architecture

### Single-file core
`digest.py` is the entire pipeline in one file. This is intentional — it is a simple script for non-developers to fork and run. Do not split it into modules without a compelling reason. If it grows beyond ~1000 lines and the split is clean, propose it first.

### Brand module
`brand.py` is the single source of truth for colours. Import from it; never hardcode hex values in `digest.py` or `setup/app.py`.

### Config is user-controlled
`config.yaml` is owned by the end user (the forker). `config.example.yaml` is the template and documentation. Do not add fields to `config.example.yaml` without also handling them in `load_config()` with a sensible default. New fields must be backward-compatible — old configs without the field must not crash.

### Setup wizard is separate
`setup/` is a standalone Flask app (entrypoint: `setup/server.py`). It has its own `requirements.txt`. Changes to `setup/` must not require changes to the core `digest.py` runtime, and vice versa.

---

## What Is Off-Limits

- **Do not add runtime dependencies** without a strong case. The current dependency list (`anthropic`, `pyyaml`, `google-generativeai`) is deliberately minimal. Every new package is a burden for users who pip-install manually.
- **Do not change the scoring cascade order** (Claude → Gemini → keyword fallback) without updating both `analyse_papers()` and the AI Scoring Tiers table in README.
- **Do not touch `config.yaml`** — it is user data, not project config. Never commit changes to it. It is gitignored for a reason (or should be — check).
- **Do not remove the keyword-only fallback** — it is the zero-dependency path that works without any API key.

---

## Coding Style

- Python 3.12+ in CI (see workflow). Local dev: use the Homebrew Python at `/opt/homebrew/bin/python3.13`.
- Type hints on all public functions. Use `from __future__ import annotations` at the top of new files.
- Docstrings on public functions. One-liner if the name is obvious; multi-line if the behaviour is non-trivial.
- No trivial comments. Comments explain *why*, not *what*.
- Section dividers use the project style: `# ─────── Section Name ─────────────` (em-dash lines, 60 chars).
- `load_config()` is the config validation boundary. All defaults go there. Downstream functions trust the config dict is valid.

---

## Testing

- **Test-first (TDD) is required.** Write a failing test before any implementation code. Watch it fail. Then implement. Tests written after the fact pass immediately and prove nothing.
- Bug fix? Write the failing test that reproduces the bug first, then fix it.
- New feature? Write the failing test that specifies the behaviour first, then build it.
- Test suite: `tests/test_digest.py` (pytest). Run with `pytest tests/`.
- Tests use `unittest.mock.patch` to isolate file I/O and env vars. Never let tests hit the real filesystem or make real API calls.
- Use `make_paper()` and `make_config()` fixture helpers for minimal valid dicts.
- Known bugs with wrong-but-documented behaviour are marked `@pytest.mark.xfail` with an explanation.
- CI runs tests on every push (check `.github/workflows/` for the current setup).

---

## GitHub Actions

- The workflow file is `.github/workflows/digest.yml`.
- All `uses:` lines must be pinned to a commit SHA, not a mutable tag. Format: `uses: actions/foo@<sha>  # vN`.
- Secrets follow the naming convention in the workflow: `SMTP_USER`, `SMTP_PASSWORD`, `RECIPIENT_EMAIL`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`. Legacy names (`GMAIL_USER`, `GMAIL_APP_PASSWORD`) are supported for backward compat but should not be promoted in docs.

---

## Requirements Pinning

- `requirements.txt` pins all dependencies to exact versions (`==`). No ranges.
- When updating a version, check that the tests still pass.
- `setup/requirements.txt` is separate and may have different versions.

---

## README and Docs

- README is for end users (forkers). It must stay non-technical. No jargon, no internal architecture details.
- Operational rules for contributors go in `CONTRIBUTING.md`, not the README.
- The README's Quick Start section is the canonical onboarding path. Keep it to 6 steps.

---

## Email Formatting

- The subject line uses a telescope emoji (🔭) as prefix. This is intentional for brand recognition but may trigger corporate email filters. If users report delivery issues, the emoji is the first thing to investigate.
