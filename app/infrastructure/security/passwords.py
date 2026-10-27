import anyio
import bcrypt


async def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bool(password_hash) and await anyio.to_thread.run_sync(
            bcrypt.checkpw, password.encode(), password_hash.encode()
        )
    except (ValueError, TypeError):
        return False
