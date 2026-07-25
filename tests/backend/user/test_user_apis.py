from backend.auth.schemas import Token
from httpx import AsyncClient


async def login(client: AsyncClient, email: str, password: str) -> str:
    response = await client.post("/api/v1/auth/jwt/login", data={"username": email, "password": password})
    assert response.status_code == 200
    return Token(**response.json()).access_token


async def test_create_user(client: AsyncClient):
    token = await login(client, "test@example.com", "devpassword123")

    response = await client.post(
        "/api/v1/users",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "Bob", "email": "bob@example.com", "password": "securepassword"},
    )
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "Bob"
    assert data["email"] == "bob@example.com"
    assert "id" in data


async def test_create_user_unauthorized(client: AsyncClient):
    response = await client.post(
        "/api/v1/users",
        json={"name": "Bob", "email": "bob@example.com", "password": "securepassword"},
    )
    assert response.status_code == 401


async def test_retrieve_user_me(client: AsyncClient):
    token = await login(client, "test@example.com", "devpassword123")

    response = await client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    data = response.json()
    assert data["email"] == "test@example.com"
