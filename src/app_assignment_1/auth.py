"""The operator token check, shared by every operator route.

Caddy proxies /v1, so the token is the only guard on these routes. The rules
match PR #84 (the release route): 403 while APP_OPERATOR_TOKEN is unset, 401
for a missing or wrong token.
"""

import secrets
from typing import Annotated

from fastapi import Header, HTTPException, Request


def require_operator(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> None:
    """Allow only callers with the operator token. A dependency, so it runs
    before the body is validated: without the token, a bad body gets 401 or
    403, not the schema's error messages."""
    token = request.app.state.operator_token
    if token is None:
        raise HTTPException(
            403, "Operator routes are turned off; set APP_OPERATOR_TOKEN"
        )
    scheme, _, value = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(
        value.strip().encode(), token.get_secret_value().encode()
    ):
        raise HTTPException(
            401, "Missing or wrong operator token", {"WWW-Authenticate": "Bearer"}
        )
