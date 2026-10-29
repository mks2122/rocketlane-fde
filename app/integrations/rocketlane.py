"""Rocketlane adapter (implements `ProjectStore`) on the REST API.

Verified against developer.rocketlane.com and the workspace (Oct 2026):
- base `https://api.rocketlane.com/api/1.0`, header `api-key`;
- `GET /projects` pages with `pageSize` + `pageToken` → `pagination.nextPageToken/hasMore`;
- `POST /projects` takes `projectName`, `owner`, `customer`, `startDate`,
  `sources[{templateId, startDate}]`, `teamMembers`, `placeholders`, `fields`,
  `assignProjectOwner`, `autoCreateCompany` and `externalReferenceId`;
- with a template source, Rocketlane computes the due date from the template's
  durations in working days and ignores a `dueDate` we send, so we don't send one;
- creating from a template can take more than 20 s (live test), hence
  ROCKETLANE_READ_TIMEOUT_SECONDS;
- 4xx bodies are `{"errors": [{"errorCode", "errorMessage", "field"}]}`.

`externalReferenceId` carries our deal id: the idempotency marker that lets a retry find
a project whose create timed out but succeeded.
"""

from __future__ import annotations

from typing import Any, Final, cast

import structlog

from app.config import Settings
from app.errors import PermanentError
from app.integrations.http import HttpService
from app.ports import NewProject, ProjectRef

log = structlog.get_logger(__name__)

SERVICE: Final = "rocketlane"
PAGE_SIZE: Final = 100
MAX_PAGES: Final = 50  # 5,000 projects; far beyond this customer base


def project_ref(raw: dict[str, Any]) -> ProjectRef:
    customer = cast(dict[str, Any], raw.get("customer") or {})
    return ProjectRef(
        project_id=str(raw["projectId"]),
        name=str(raw.get("projectName") or ""),
        customer_company=customer.get("companyName"),
        deal_marker=raw.get("externalReferenceId") or None,
        is_active=not raw.get("archived", False),
    )


def build_create_request(project: NewProject) -> dict[str, Any]:
    start = project.start_date.isoformat()
    team: dict[str, Any] = {"members": [{"emailId": project.csm_email}]}
    if project.customer_contact_email:
        team["customers"] = [{"emailId": project.customer_contact_email}]
    body: dict[str, Any] = {
        "projectName": project.project_name,
        "owner": {"emailId": project.owner_email},
        "customer": {"companyName": project.customer_company},
        "autoCreateCompany": True,  # reuses the company if it already exists
        "startDate": start,
        "sources": [{"templateId": project.template_id, "startDate": start}],
        "teamMembers": team,
        "assignProjectOwner": project.assign_unassigned_tasks_to_owner,
        "externalReferenceId": project.deal_id,
    }
    if project.csm_placeholder_id is not None:
        body["placeholders"] = [
            {"placeholderId": project.csm_placeholder_id, "user": {"emailId": project.csm_email}}
        ]
    if project.custom_fields:
        body["fields"] = [
            {"fieldId": fid, "fieldValue": value} for fid, value in project.custom_fields
        ]
    return body


class RocketlaneProjects:
    def __init__(self, settings: Settings, *, http: HttpService | None = None) -> None:
        settings.require("rocketlane_api_key")
        assert settings.rocketlane_api_key is not None  # noqa: S101  # checked by require()
        self._http = http or HttpService.create(
            settings,
            name=SERVICE,
            base_url=settings.rocketlane_base_url,
            headers={
                "api-key": settings.rocketlane_api_key.get_secret_value(),
                "accept": "application/json",
            },
            read_timeout_seconds=settings.rocketlane_read_timeout_seconds,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def list_projects(self) -> list[ProjectRef]:
        projects: list[ProjectRef] = []
        token: str | None = None
        for _ in range(MAX_PAGES):
            params: dict[str, Any] = {"pageSize": PAGE_SIZE, "includeFields": "externalReferenceId"}
            if token:
                params["pageToken"] = token
            body = cast(
                dict[str, Any], (await self._http.request("GET", "/projects", params=params)).json()
            )
            projects.extend(
                project_ref(p) for p in cast(list[dict[str, Any]], body.get("data") or [])
            )
            pagination = cast(dict[str, Any], body.get("pagination") or {})
            token = pagination.get("nextPageToken")
            if not pagination.get("hasMore") or not token:
                return projects
        # Returning a partial list would let the duplicate check miss a project.
        raise PermanentError(
            f"{SERVICE}: more than {MAX_PAGES * PAGE_SIZE} projects; raise MAX_PAGES"
        )

    async def create_project(self, project: NewProject) -> ProjectRef:
        response = await self._http.request(
            "POST", "/projects", json=build_create_request(project), idempotent=False
        )
        body = cast(dict[str, Any], response.json())
        if "projectId" not in body:
            raise PermanentError(f"{SERVICE}: POST /projects returned no projectId")
        ref = project_ref(body)
        # Some responses omit fields we didn't ask for; we know what we sent.
        return ref.model_copy(
            update={"deal_marker": project.deal_id, "customer_company": project.customer_company}
        )
