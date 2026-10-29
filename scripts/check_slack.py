"""Read-only: check the Slack bot token, its scopes, and the escalation channel.

Also looks up the CSMs and project owners from config/templates.yaml, since anyone
without a Slack account is skipped when a channel is opened. Writes nothing.

    uv run python scripts/check_slack.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Final, cast

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings, load_templates
from app.errors import AppError
from app.integrations.http import HttpService

REQUIRED_SCOPES: Final = frozenset(
    {"channels:read", "channels:manage", "chat:write", "users:read", "users:read.email"}
)


async def _call(slack: HttpService, method: str, **params: Any) -> tuple[dict[str, Any], str]:
    response = await slack.request("GET", f"/{method}", params=params)
    return cast(dict[str, Any], response.json()), response.headers.get("x-oauth-scopes", "")


async def _find_channel(slack: HttpService, name: str) -> dict[str, Any] | None:
    cursor = ""
    for _ in range(20):
        body, _ = await _call(
            slack,
            "conversations.list",
            types="public_channel",
            exclude_archived="true",
            limit=200,
            cursor=cursor,
        )
        for channel in body.get("channels") or []:
            if channel.get("name") == name:
                return cast(dict[str, Any], channel)
        cursor = (body.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break
    return None


async def main() -> int:
    settings = get_settings()
    problems: list[str] = []
    try:
        settings.require("slack_bot_token")
        assert settings.slack_bot_token is not None  # noqa: S101  # checked by require()
        config = load_templates(settings.templates_path)
        slack = HttpService.create(
            settings,
            name="slack",
            base_url=settings.slack_base_url,
            headers={"Authorization": f"Bearer {settings.slack_bot_token.get_secret_value()}"},
        )
        try:
            auth, scopes = await _call(slack, "auth.test")
            if not auth.get("ok"):
                print(f"Token rejected: {auth.get('error')}")
                return 1
            print(f"Workspace: {auth.get('team')} ({auth.get('url')})  bot: @{auth.get('user')}")
            granted = {s.strip() for s in scopes.split(",") if s.strip()}
            if missing := sorted(REQUIRED_SCOPES - granted):
                problems.append(f"missing scopes: {', '.join(missing)} (add them, reinstall)")

            name = settings.slack_escalation_channel
            channel = await _find_channel(slack, name)
            if channel is None:
                problems.append(f"#{name} not found: create it, then /invite the bot")
            elif not channel.get("is_member"):
                problems.append(f"bot is not in #{name}: /invite @{auth.get('user')} there")
            else:
                print(f"#{name}: found, bot is a member")

            people = {config.enterprise.project_owner, config.growth.project_owner}
            people |= set(config.enterprise.csm_pool) | set(config.growth.csm_pool)
            for email in sorted(people):
                body, _ = await _call(slack, "users.lookupByEmail", email=email)
                status = "in Slack" if body.get("ok") else f"not found ({body.get('error')})"
                print(f"  {email}: {status}")
        finally:
            await slack.aclose()
    except AppError as exc:
        print(f"Check failed: {exc}")
        return 1

    if problems:
        print("\nProblems:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nSlack is ready. (People not in Slack are skipped when channels open.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
