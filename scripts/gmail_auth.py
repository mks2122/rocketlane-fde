"""One-time Gmail authorisation for the CS inbox.

Opens a browser for Google consent, then writes the refresh token to GMAIL_TOKEN_PATH
(atomically, via the same TokenStore the app uses). Re-run it whenever /health reports
the Gmail token as revoked or expired. Apps left in "Testing" mode get refresh tokens
that expire after 7 days.

    uv run python scripts/gmail_auth.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google_auth_oauthlib.flow import InstalledAppFlow

from app.config import get_settings
from app.integrations.gmail import SCOPES, TokenStore


def main() -> int:
    settings = get_settings()
    client_secrets = settings.gmail_credentials_path
    if not client_secrets.is_file():
        print(
            f"Missing OAuth client file: {client_secrets}\n"
            "Create one in Google Cloud Console > APIs & Services > Credentials > "
            "OAuth client ID > Desktop app, and save the JSON there."
        )
        return 1

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), list(SCOPES))
    # access_type=offline + prompt=consent guarantee Google returns a refresh token,
    # even when this account authorised the app before.
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    TokenStore(settings.gmail_token_path).save(creds)  # pyright: ignore[reportArgumentType]
    print(f"Saved Gmail token to {settings.gmail_token_path}. Set GMAIL_ENABLED=true in .env.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
