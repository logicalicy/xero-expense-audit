# xero-expense-audit

> **Disclaimer:** This tool is provided "as is", without warranty of any kind,
> express or implied. Use at your own risk. The author(s) accept no liability for
> any damages, data loss, or incorrect financial transactions resulting from the
> use of this software. Always verify changes in Xero before approving. This is
> not a substitute for professional accounting advice.

A CLI tool that validates Xero bills and bank transactions, flags missing or incorrect fields, and queues suggested corrections for interactive review before anything touches your books. Most fixes are deterministic (copy dates, look up supplier defaults, convert FX via ECB rates). When structured data isn't enough, Claude Haiku reads receipt images, extracts line descriptions, and infers expense categories.

## Prerequisites

- Python 3.12+
- A [Xero OAuth 2.0 app](https://developer.xero.com/app/manage/) (Web App type)
- An [Anthropic API key](https://console.anthropic.com/) (uses Claude Haiku)

## Setup

```bash
# Install dependencies.
uv sync

# Copy and fill in environment variables.
cp .env.example .env
# Edit .env with your Xero client ID/secret and Anthropic API key.

# Authenticate with Xero (opens browser).
uv run audit.py setup-auth
```

## Commands

| Command                                         | Description                                                     |
| ----------------------------------------------- | --------------------------------------------------------------- |
| `uv run audit.py run`                           | Fetch bills from Xero, validate, flag issues, queue corrections |
| `uv run audit.py run --auto-correct`            | Same as above, but auto-fix where strategy chains succeed       |
| `uv run audit.py run --fix-suppliers`           | Infer and set missing supplier default accounts                 |
| `uv run audit.py review`                        | Interactively review queued corrections (approve/reject/skip)   |
| `uv run audit.py status`                        | Show queue summary                                              |
| `uv run audit.py clear`                         | Remove approved items from the queue                            |
| `uv run audit.py fix-bill`                      | Fix a single bill interactively                                 |
| `uv run audit.py fix-next-bill`                 | Loop through fixable bills one at a time                        |
| `uv run audit.py mark-as-director-loan-payment` | Mark authorised bills as paid from Director Loan Account        |
| `uv run audit.py director-loan-balance`         | Show Director Loan Account balance summary                      |
| `uv run audit.py setup-auth`                    | One-time OAuth 2.0 browser login                                |
| `uv run audit.py skip`                          | Add an exception (skip a bill from future audits)               |
| `uv run audit.py exceptions`                    | List current exceptions                                         |

## How AI is used

Most validation and correction is deterministic — Claude Haiku is only pulled in when inference is needed:

1. **Validation** — configurable rules check for missing fields, invalid tax rates, duplicates, and amount anomalies (`config/rules.yaml`).
2. **Strategy chains** — each issue type has an ordered chain of fix strategies (copy bill date, read supplier defaults, look up tax mappings, convert FX via ECB rates).
3. **Claude Haiku** — when structured data isn't enough, Haiku reads receipt/invoice images via vision, extracts line descriptions from attachments, and infers expense categories from context.

AI suggestions always go through the review queue unless you explicitly pass `--auto-correct`.

## Configuration

| File                      | Purpose                                                                        |
| ------------------------- | ------------------------------------------------------------------------------ |
| `config/xero.config.json` | Auto-correct strategy chains, account-to-tax mappings, supplier audit settings |
| `config/rules.yaml`       | Validation thresholds (duplicate window, FX deviation, account code ranges)    |
| `config/ai.yaml`          | Model selection and confidence thresholds                                      |
| `.env`                    | API credentials and behaviour flags                                            |

## License

MIT — see [LICENSE](LICENSE) for details. This software is provided without
warranty. Use at your own risk.
