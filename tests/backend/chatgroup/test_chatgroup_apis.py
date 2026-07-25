import uuid

from backend.auth.schemas import Token
from httpx import AsyncClient


async def login(client: AsyncClient, email: str, password: str) -> str:
    response = await client.post("/api/v1/auth/jwt/login", data={"username": email, "password": password})
    assert response.status_code == 200
    return Token(**response.json()).access_token


async def _create_chatgroup(client: AsyncClient, token: str) -> dict:
    response = await client.post(
        "/api/v1/chatgroups",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "Test Group", "description": "A test group"},
    )
    assert response.status_code == 201
    return response.json()


async def _create_other_user(client: AsyncClient, token: str) -> tuple[str, str]:
    email = f"other-{uuid.uuid4()}@example.com"
    response = await client.post(
        "/api/v1/users",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "Other User", "email": email, "password": "securepassword123"},
    )
    assert response.status_code == 201
    return email, "securepassword123"


async def test_create_chatgroup(client: AsyncClient):
    token = await login(client, "test@example.com", "devpassword123")

    response = await client.post(
        "/api/v1/chatgroups",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "Test Group", "description": "A test group"},
    )
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "Test Group"
    assert data["description"] == "A test group"
    assert "id" in data


async def test_retrieve_chatgroup(client: AsyncClient):
    token = await login(client, "test@example.com", "devpassword123")
    chatgroup_data = await _create_chatgroup(client, token)

    response = await client.get(
        f"/api/v1/chatgroups/{chatgroup_data['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json() == chatgroup_data


async def test_update_chatgroup(client: AsyncClient):
    token = await login(client, "test@example.com", "devpassword123")
    chatgroup_data = await _create_chatgroup(client, token)

    response = await client.patch(
        f"/api/v1/chatgroups/{chatgroup_data['id']}",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "Updated Group"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Updated Group"
    assert data["id"] == chatgroup_data["id"]


async def test_delete_chatgroup(client: AsyncClient):
    token = await login(client, "test@example.com", "devpassword123")
    chatgroup_data = await _create_chatgroup(client, token)

    response = await client.delete(
        f"/api/v1/chatgroups/{chatgroup_data['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 204

    response = await client.get(
        f"/api/v1/chatgroups/{chatgroup_data['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404


async def test_list_chatgroups(client: AsyncClient):
    token = await login(client, "test@example.com", "devpassword123")

    response = await client.get(
        "/api/v1/chatgroups",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    baseline = len(response.json()["items"])

    [await _create_chatgroup(client, token) for _ in range(3)]

    response = await client.get(
        "/api/v1/chatgroups",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data["items"]) == baseline + 3


async def test_chatgroup_not_owned(client: AsyncClient):
    token_a = await login(client, "test@example.com", "devpassword123")
    chatgroup_data = await _create_chatgroup(client, token_a)

    email_b, password_b = await _create_other_user(client, token_a)
    token_b = await login(client, email_b, password_b)

    response = await client.get(
        f"/api/v1/chatgroups/{chatgroup_data['id']}",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert response.status_code == 404
