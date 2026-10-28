"""Shared request dependencies: auth and access to the process runtime."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from pydantic import SecretStr

from app.orchestrator.runtime import Runtime


def secrets_match(provided: str | None, expected: SecretStr | None) -> bool:
    """Constant-time comparison; an unset secret matches nothing (fail closed)."""
    if not provided or expected is None:
        return False
    return hmac.compare_digest(provided.encode(), expected.get_secret_value().encode())


def get_runtime(request: Request) -> Runtime:
    runtime: Runtime = request.app.state.runtime
    return runtime


def require_admin(request: Request, x_admin_token: Annotated[str | None, Header()] = None) -> None:
    if not secrets_match(x_admin_token, get_runtime(request).settings.admin_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid admin token")


RuntimeDep = Annotated[Runtime, Depends(get_runtime)]
AdminDep = Annotated[None, Depends(require_admin)]
