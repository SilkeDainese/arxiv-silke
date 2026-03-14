# 🔭 arXiv Digest

**Your personal arXiv paper curator** — fetches new papers, scores them with AI, and delivers a beautiful HTML digest to your inbox.

Created by [Silke S. Dainese](https://silkedainese.github.io) · [dainese@phys.au.dk](mailto:dainese@phys.au.dk) · [ORCID](https://orcid.org/0009-0001-7885-2439)

I built this for myself. I am a PhD student in astronomy at Aarhus University — not a software developer — and I wanted a smarter way to stay on top of new arXiv papers without spending an hour every morning. Other people in my department found it useful, so I cleaned it up and made it public. It is primarily aimed at people in physics and astronomy, but it will work for anyone on arXiv.

If you have suggestions, open an issue or email me. I cannot promise to implement them — my research comes first.

*Built with the help of Claude Opus and Sonnet 4.6.*

---

## Quick Start (5 minutes)

### 1. Generate your config

Visit **[arxiv-digest-setup.streamlit.app](https://arxiv-digest-setup.streamlit.app)** and fill in:
- Your name and research description
- Keywords with importance weights (1-10)
- arXiv categories to monitor
- Colleagues to track

Download the generated `config.yaml`.

### 2. Fork this repo

Click **Fork** at the top of this page.

### 3. Upload your config

In your fork, go to **Add file → Upload files** and upload `config.yaml`.

### 4. Add secrets

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Value |
|--------|-------|
| `RECIPIENT_EMAIL` | Your email address |
| `SMTP_USER` | Your Gmail or Outlook address |
| `SMTP_PASSWORD` | An [App Password](#email-setup) |

**Optional AI keys** (the digest works without these, using keyword-only scoring):

| Secret | Value |
|--------|-------|
| `ANTHROPIC_API_KEY` | [Claude API key](https://console.anthropic.com/) — best scoring quality |
| `GEMINI_API_KEY` | [Gemini API key](https://aistudio.google.com/apikey) — free tier available |

### 5. Enable workflow permissions

Go to **Settings → Actions → General → Workflow permissions** and select **"Read and write permissions"**. This lets the digest save keyword tracking stats between runs.

### 6. Done!

Your digest runs automatically Mon/Wed/Fri at 9am Danish time (07:00 UTC).

To trigger it manually: **Actions → arXiv Digest → Run workflow**.

---

## Email Setup

### Gmail

1. Enable [2-Step Verification](https://myaccount.google.com/signinoptions/two-step-verification)
2. Generate an [App Password](https://myaccount.google.com/apppasswords)
3. Use the App Password as `SMTP_PASSWORD`

Config settings (default):
```yaml
smtp_server: "smtp.gmail.com"
smtp_port: 587
```

### Outlook / Office 365

1. Set up an App Password in your [Microsoft account security settings](https://account.microsoft.com/security)
2. Use the App Password as `SMTP_PASSWORD`

Config settings:
```yaml
smtp_server: "smtp.office365.com"
smtp_port: 587
```

---

## AI Scoring Tiers

The digest uses a waterfall strategy for scoring papers:

| Tier | Provider | Quality | Cost |
|------|----------|---------|------|
| 1 | **Claude** (Anthropic) | Best | Paid API — $5 lasts hundreds of digests |
| 2 | **Gemini 2.0 Flash** (Google) | Good | Free tier available |
| 3 | **Keyword fallback** | Basic | Free, no API needed |

Set `ANTHROPIC_API_KEY` and/or `GEMINI_API_KEY` in your repo secrets. The digest automatically uses the best available — if Claude runs out of credits, it cascades to Gemini, then to keyword-only scoring. No money goes to the creator of this tool; API costs go directly to Anthropic/Google for the AI scoring.

### How papers are scored

Whether you use AI or keyword-only scoring, the algorithm works the same way underneath:

1. **Keyword matching** — your keywords are checked against each paper's title and abstract, weighted by the importance you assigned them (1–10).
2. **Normalization** — scores are normalized across the day's papers so the ranking reflects relative relevance, not raw counts.
3. **Self-citation boost** — papers that cite your work, or papers you have authored yourself, receive a relevance boost. You can toggle this on or off in your config.

When AI scoring is active (Claude or Gemini), it reads your `research_context` and re-ranks papers using that description in addition to keywords. This is why a good `research_context` matters — the more specific you are, the better the scoring.

---

## Config Reference

See [`config.example.yaml`](config.example.yaml) for all options with comments.

Key fields:

| Field | Description |
|-------|-------------|
| `research_context` | Free-text description of your research (used by AI scoring) |
| `keywords` | Dictionary of `keyword: weight` pairs (1-10) |
| `categories` | arXiv categories to monitor |
| `research_authors` | Authors whose papers get a relevance boost |
| `colleagues` | People/institutions whose papers always show |
| `github_repo` | Your fork's path (enables self-service links in emails) |

---

## Managing Your Digest

Every digest email includes self-service links:

- **Edit interests** → opens `config.yaml` in GitHub's web editor
- **Pause** → links to the Actions tab (disable the workflow)
- **Re-run setup** → opens the setup wizard
- **Delete** → links to repo Settings (Danger Zone → Delete repository)

### How to Unsubscribe

1. **Pause**: Go to your repo → Actions → arXiv Digest → click ⋯ → Disable workflow
2. **Delete**: Go to your repo → Settings → scroll to Danger Zone → Delete this repository
3. **Revoke email access**: Remove the App Password from your Google/Microsoft account

---

## Development

### Run locally

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY="your-key"  # optional
export RECIPIENT_EMAIL="you@example.com"
export SMTP_USER="you@gmail.com"
export SMTP_PASSWORD="your-app-password"
python digest.py
```

### Run the setup wizard locally

```bash
cd setup
pip install -r requirements.txt
streamlit run app.py
```

---

## License

MIT — see [LICENSE](LICENSE).

**Created by [Silke S. Dainese](https://silkedainese.github.io)** · Aarhus University · Dept. of Physics & Astronomy
