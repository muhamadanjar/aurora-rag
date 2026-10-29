from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm

from app.application.auth.service import LoginService
from app.core.config import Settings
from app.core.exceptions import InvalidCredentials, TooManyLoginAttempts
from app.delivery.dependencies import require_settings
from app.delivery.schemas.auth import TokenResponse
from app.infrastructure.security.passwords import verify_password
from app.infrastructure.security.tokens import create_access_token

router = APIRouter(tags=["auth"])


@router.post("/auth/token", response_model=TokenResponse)
async def token(
    request: Request,
    form: OAuth2PasswordRequestForm = Depends(),
    settings: Settings = Depends(require_settings),
):
    client_key = request.client.host if request.client else "unknown"
    try:
        access_token = await LoginService(
            settings,
            verify_password=verify_password,
            issue_token=lambda cfg: create_access_token(cfg.auth_username, cfg),
        ).login(form.username, form.password, client_key)
    except TooManyLoginAttempts as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts",
            headers={"Retry-After": "60"},
        ) from exc
    except InvalidCredentials as exc:
        raise HTTPException(status_code=401, detail="Invalid credentials") from exc
    return TokenResponse(access_token=access_token)
