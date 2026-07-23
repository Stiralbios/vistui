from argon2 import PasswordHasher
from pydantic import SecretStr

_password_hasher = PasswordHasher()


def hash_password(password: SecretStr | str) -> str:
    if isinstance(password, SecretStr):
        password = password.get_secret_value()
    return _password_hasher.hash(password)


def verify_password(password: SecretStr | str, hashed_password: str) -> bool:
    if isinstance(password, SecretStr):
        password = password.get_secret_value()
    try:
        return _password_hasher.verify(hashed_password, password)
    except Exception:
        return False
