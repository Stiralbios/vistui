# Vistui Authentication

Authentication and access control for the Vistui backend.

## Overview

Vistui v1 uses lightweight JWT-based authentication. Every operation besides login requires a valid user token. All authenticated users share the same rights in v1; there is no per-resource authorization beyond user scoping.

- **Protocol**: OAuth2 Password Flow.
- **Tokens**: JWT access tokens.
- **Password Hashing**: Argon2id via `argon2-cffi`.
- **Token Lifespan**: configured via `JWT_ACCESS_TOKEN_EXPIRATION_MINUTES` (default: 60 min).

## Authentication Flow

1. Client sends credentials to `/api/v1/auth/jwt/login`.
2. Server validates credentials (email + password).
3. Server returns JWT access token.
4. Client sends token in `Authorization: Bearer <token>` header.
5. Server validates token on protected endpoints.

## Endpoints

### POST `/api/v1/auth/jwt/login`

OAuth2 password login. Accepts `application/x-www-form-urlencoded`:

```
username=<email>
password=<password>
```

Response:

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "bearer"
}
```

## Dependencies

### `get_current_user`

Extracts and validates JWT token from `Authorization: Bearer <token>` header.

```python
async def get_current_user(token: Annotated[str, Depends(oauth2_scheme)])
```

- Decodes token using `JWT_SECRET_KEY`.
- Extracts `sub` claim (email).
- Retrieves user from database.
- Raises 401 if invalid or expired.

### `get_current_active_user`

Extends `get_current_user` to also check `is_active` flag.

```python
async def get_current_active_user(
    current_user: Annotated[UserInternal, Depends(get_current_user)]
)
```

Raises 400 "Inactive user" if the user is deactivated.

Usage in endpoints:

```python
@router.get("/me")
async def me(user: UserInternal = Depends(get_current_active_user)):
    return user
```

## Token Generation

Handled by `AuthService.create_access_token()`:

```python
async def create_access_token(self, data: dict, expires_delta: timedelta | None = None):
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + expires_delta
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)
    return encoded_jwt
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `JWT_ALGORITHM` | `HS256` | Token signing algorithm. |
| `JWT_SECRET_KEY` | required | HS256 secret key. |
| `JWT_ACCESS_TOKEN_EXPIRATION_MINUTES` | `60` | Token validity duration. |

## Password Security

- Passwords are hashed with Argon2id using `argon2-cffi`.
- `hash_password()` accepts `SecretStr` or `str`.
- `verify_password()` validates against stored hash.
- `SecretStr` is unwrapped with `.get_secret_value()` before hashing.

## Access Control Notes

- v1 does not implement resource-level authorization.
- Any authenticated user can read or write any User, ChatGroup, Chat, or Message.
- Documentation includes a warning that right management is lacking in v1.

## Files

- `sources/backend/auth/apis.py` — login endpoint.
- `sources/backend/auth/services.py` — token creation and password hashing.
- `sources/backend/auth/dependencies.py` — JWT validation.
- `sources/backend/auth/schemas.py` — token schemas.
