"""Read-only: check config/templates.yaml against the Rocketlane workspace.

Lists project templates and users, then reports anything that would make project
creation fail or pick the wrong template. Writes nothing.

    uv run python scripts/check_rocketlane.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings, load_templates
from app.errors import AppError
from app.integrations.http import HttpService
from app.services.rocketlane_check import check_config


async def _all(rl: HttpService, path: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    token: str | None = None
    for _ in range(20):
        params: dict[str, Any] = {"pageSize": 100}
        if token:
            params["pageToken"] = token
        body = (await rl.request("GET", path, params=params)).json()
        items += body.get("data") or []
        pagination = body.get("pagination") or {}
        token = pagination.get("nextPageToken")
        if not pagination.get("hasMore") or not token:
            break
    return items


async def main() -> int:
    settings = get_settings()
    try:
        settings.require("rocketlane_api_key")
        assert settings.rocketlane_api_key is not None  # noqa: S101  # checked by require()
        config = load_templates(settings.templates_path)
        rl = HttpService.create(
            settings,
            name="rocketlane",
            base_url=settings.rocketlane_base_url,
            headers={"api-key": settings.rocketlane_api_key.get_secret_value()},
        )
        try:
            templates, users = await _all(rl, "/templates"), await _all(rl, "/users")
        finally:
            await rl.aclose()
    except AppError as exc:
        print(f"Check failed: {exc}")
        return 1

    print("Project templates in the workspace:")
    for template in templates:
        if template.get("objectType") == "PROJECT":
            print(f"  {template['templateId']}  {template['templateName']}")
    problems = check_config(config, templates=templates, users=users)
    if problems:
        print("\nProblems:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\ntemplates.yaml matches the workspace.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
