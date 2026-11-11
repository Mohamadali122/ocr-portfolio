from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from app.api.deps import AuthServiceDep, CurrentUser, client_ip
from app.core.config import get_settings
from app.core.rate_limit import limiter
from app.schemas.auth import LoginRequest, RefreshRequest, TokenPair, UserRead

router = APIRouter(prefix="/auth", tags=["auth"])

# 255 chars: the column width. A 40 KB User-Agent should not blow up an INSERT.
_UA_MAX = 255


@router.post("/login", response_model=TokenPair)
@limiter.limit(lambda: f"{get_settings().rate_limit_login_per_minute}/minute")
def login(
    payload: LoginRequest,
    request: Request,
    auth: AuthServiceDep,
    ip: Annotated[str | None, Depends(client_ip)],
) -> TokenPair:
    """Returns 401 with one generic message for every failure mode.

    Rate limited per client IP (`Settings.rate_limit_login_per_minute`) -- the
    brute-force gap this endpoint's docstring used to name is closed. `request`
    must stay a plain parameter: slowapi's decorator inspects the signature for
    it.
    """
    return auth.authenticate(
        email=payload.email,
        password=payload.password,
        ip_address=ip,
        user_agent=request.headers.get("user-agent", "")[:_UA_MAX] or None,
    )


@router.post("/refresh", response_model=TokenPair)
def refresh(
    payload: RefreshRequest,
    auth: AuthServiceDep,
    ip: Annotated[str | None, Depends(client_ip)],
) -> TokenPair:
    """Exchanges a refresh token for a new pair.

    The old refresh token stays valid until it expires -- this is rotation
    without invalidation. Bumping `users.token_version` is the revocation lever.
    """
    return auth.refresh(refresh_token=payload.refresh_token, ip_address=ip)


@router.get("/me", response_model=UserRead, status_code=status.HTTP_200_OK)
def me(user: CurrentUser) -> UserRead:
    return UserRead.model_validate(user)
