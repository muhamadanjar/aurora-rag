from collections import deque
from collections.abc import Awaitable, Callable
from time import monotonic

from app.core.config import Settings
from app.core.exceptions import InvalidCredentials, TooManyLoginAttempts

_LOGIN_WINDOW_SECONDS = 60.0
_LOGIN_MAX_FAILURES = 5
_LOGIN_MAX_KEYS = 1024

_failed_logins: dict[str, deque[float]] = {}


def reserve_login_attempt(key: str) -> bool:
    now = monotonic()
    attempts = _failed_logins.get(key)
    if attempts is not None:
        while attempts and now - attempts[0] > _LOGIN_WINDOW_SECONDS:
            attempts.popleft()
        if not attempts:
            _failed_logins.pop(key, None)
            attempts = None
        elif len(attempts) >= _LOGIN_MAX_FAILURES:
            return False
    if attempts is None:
        if len(_failed_logins) >= _LOGIN_MAX_KEYS:
            oldest_key = min(_failed_logins, key=lambda item: _failed_logins[item][0])
            _failed_logins.pop(oldest_key)
        attempts = deque()
        _failed_logins[key] = attempts
    attempts.append(now)
    return True


def clear_failed_logins(key: str) -> None:
    _failed_logins.pop(key, None)


class LoginService:
    def __init__(
        self,
        settings: Settings,
        *,
        verify_password: Callable[[str, str], Awaitable[bool]],
        issue_token: Callable[[Settings], str],
    ) -> None:
        self._settings = settings
        self._verify_password = verify_password
        self._issue_token = issue_token

    async def login(self, username: str, password: str, client_key: str) -> str:
        if not reserve_login_attempt(client_key):
            raise TooManyLoginAttempts()
        if username != self._settings.auth_username or not await self._verify_password(
            password, self._settings.auth_password_hash
        ):
            raise InvalidCredentials()
        clear_failed_logins(client_key)
        return self._issue_token(self._settings)
