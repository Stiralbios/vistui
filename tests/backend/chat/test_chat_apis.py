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


async def _create_chat(client: AsyncClient, token: str, chatgroup_id: uuid.UUID | None = None, **kwargs) -> dict:
    payload = {"name": kwargs.get("name", "Test Chat"), "description": kwargs.get("description", "A chat")}
    if chatgroup_id is not None:
        payload["chatgroup_id"] = str(chatgroup_id)
    response = await client.post(
        "/api/v1/chats",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
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


async def test_create_chat_with_chatgroup(client: AsyncClient):
    token = await login(client, "test@example.com", "devpassword123")
    chatgroup = await _create_chatgroup(client, token)

    response = await client.post(
        "/api/v1/chats",
        headers={"Authorization": f"Bearer {token}"},
        json={"chatgroup_id": chatgroup["id"], "name": "Test Chat", "description": "A chat"},
    )
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "Test Chat"
    assert data["chatgroup_id"] == chatgroup["id"]


async def test_create_chat_without_chatgroup(client: AsyncClient):
    token = await login(client, "test@example.com", "devpassword123")

    response = await client.post(
        "/api/v1/chats",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "Standalone Chat", "description": "Auto group"},
    )
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "Standalone Chat"
    assert data["chatgroup_id"] is not None


async def test_retrieve_chat(client: AsyncClient):
    token = await login(client, "test@example.com", "devpassword123")
    chatgroup = await _create_chatgroup(client, token)
    chat = await _create_chat(client, token, chatgroup_id=chatgroup["id"])

    response = await client.get(
        f"/api/v1/chats/{chat['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json() == chat


async def test_update_chat(client: AsyncClient):
    token = await login(client, "test@example.com", "devpassword123")
    chatgroup = await _create_chatgroup(client, token)
    chat = await _create_chat(client, token, chatgroup_id=chatgroup["id"])

    response = await client.patch(
        f"/api/v1/chats/{chat['id']}",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "Updated Chat"},
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Updated Chat"


async def test_delete_chat(client: AsyncClient):
    token = await login(client, "test@example.com", "devpassword123")
    chatgroup = await _create_chatgroup(client, token)
    chat = await _create_chat(client, token, chatgroup_id=chatgroup["id"])

    response = await client.delete(
        f"/api/v1/chats/{chat['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 204


async def test_list_chats_by_chatgroup(client: AsyncClient):
    token = await login(client, "test@example.com", "devpassword123")
    chatgroup = await _create_chatgroup(client, token)
    [await _create_chat(client, token, chatgroup_id=chatgroup["id"], name=f"Chat {i}") for i in range(3)]

    response = await client.get(
        f"/api/v1/chats/chatgroups/{chatgroup['id']}/chats",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert len(response.json()["items"]) == 3


async def test_chat_not_owned(client: AsyncClient):
    token_a = await login(client, "test@example.com", "devpassword123")
    chatgroup = await _create_chatgroup(client, token_a)
    chat = await _create_chat(client, token_a, chatgroup_id=chatgroup["id"])

    email_b, password_b = await _create_other_user(client, token_a)
    token_b = await login(client, email_b, password_b)

    response = await client.get(
        f"/api/v1/chats/{chat['id']}",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert response.status_code == 403
