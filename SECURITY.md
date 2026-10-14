# Security

## Reporting a vulnerability

Please don't open a public issue. Use GitHub's private vulnerability reporting
(the **Report a vulnerability** button on the Security tab) with steps to
reproduce. You should get a response within a week.

## How OptionDesk handles secrets

- Credentials are read only from environment variables or a git-ignored `.env`
  file. The config loader refuses any setting that looks like a credential.
- Schwab OAuth tokens are written to `~/.optiondesk/schwab_tokens.json` with
  owner-only permissions (`0600`), outside the repository.
- Tokens, client secrets and token-endpoint response bodies are never logged.
- CI runs [gitleaks](https://github.com/gitleaks/gitleaks) on every push and pull
  request to catch committed secrets.

## Running it safely

- Keep the dashboard on `127.0.0.1` (the default). If it must be reachable from
  elsewhere, set `OPTIONDESK_API_KEY` and put it behind a TLS reverse proxy.
- Live trading requires both `mode = "live"` in the config and
  `OPTIONDESK_LIVE_TRADING=yes-i-understand` in the environment.
- If a Schwab secret or token leaks, regenerate the app secret on
  developer.schwab.com and delete the token file.
