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


async def _create_chat(client: AsyncClient, token: str, chatgroup_id: uuid.UUID) -> dict:
    response = await client.post(
        "/api/v1/chats",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "Test Chat", "description": "A chat", "chatgroup_id": str(chatgroup_id)},
    )
    assert response.status_code == 201
    return response.json()


async def _create_message(client: AsyncClient, token: str, chat_id: uuid.UUID) -> dict:
    response = await client.post(
        f"/api/v1/chats/{chat_id}/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={"full_text": "A message", "role": "user"},
    )
    assert response.status_code == 201
    return response.json()


async def _create_topic(client: AsyncClient, token: str, chatgroup_id: uuid.UUID, full_text: str = "A topic") -> dict:
    response = await client.post(
        f"/api/v1/topics?chatgroup_id={chatgroup_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "full_text": full_text,
            "start_at": "2026-07-25T10:00:00Z",
            "last_edited_at": "2026-07-25T10:00:00Z",
        },
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


async def test_create_topic(client: AsyncClient):
    token = await login(client, "test@example.com", "devpassword123")
    chatgroup = await _create_chatgroup(client, token)

    response = await client.post(
        f"/api/v1/topics?chatgroup_id={chatgroup['id']}",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "full_text": "A topic",
            "start_at": "2026-07-25T10:00:00Z",
            "last_edited_at": "2026-07-25T10:00:00Z",
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["full_text"] == "A topic"
    assert data["chatgroup_id"] == chatgroup["id"]


async def test_retrieve_topic(client: AsyncClient):
    token = await login(client, "test@example.com", "devpassword123")
    chatgroup = await _create_chatgroup(client, token)
    topic = await _create_topic(client, token, chatgroup_id=chatgroup["id"])

    response = await client.get(
        f"/api/v1/topics/{topic['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == topic["id"]
    assert data["messages_linked_count"] == 0


async def test_update_topic(client: AsyncClient):
    token = await login(client, "test@example.com", "devpassword123")
    chatgroup = await _create_chatgroup(client, token)
    topic = await _create_topic(client, token, chatgroup_id=chatgroup["id"])

    response = await client.patch(
        f"/api/v1/topics/{topic['id']}",
        headers={"Authorization": f"Bearer {token}"},
        json={"full_text": "Updated topic"},
    )
    assert response.status_code == 200
    assert response.json()["full_text"] == "Updated topic"


async def test_delete_topic(client: AsyncClient):
    token = await login(client, "test@example.com", "devpassword123")
    chatgroup = await _create_chatgroup(client, token)
    topic = await _create_topic(client, token, chatgroup_id=chatgroup["id"])

    response = await client.delete(
        f"/api/v1/topics/{topic['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 204


async def test_list_topics_by_chatgroup(client: AsyncClient):
    token = await login(client, "test@example.com", "devpassword123")
    chatgroup = await _create_chatgroup(client, token)
    [await _create_topic(client, token, chatgroup_id=chatgroup["id"], full_text=f"Topic {i}") for i in range(3)]

    response = await client.get(
        f"/api/v1/topics/chatgroups/{chatgroup['id']}/topics",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert len(response.json()["items"]) == 3


async def test_link_message_to_topic(client: AsyncClient):
    token = await login(client, "test@example.com", "devpassword123")
    chatgroup = await _create_chatgroup(client, token)
    topic = await _create_topic(client, token, chatgroup_id=chatgroup["id"])
    chat = await _create_chat(client, token, chatgroup_id=chatgroup["id"])
    message = await _create_message(client, token, chat_id=chat["id"])

    response = await client.post(
        f"/api/v1/topics/{topic['id']}/messages/{message['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 204

    response = await client.get(
        f"/api/v1/topics/{topic['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json()["messages_linked_count"] == 1

    response = await client.post(
        f"/api/v1/topics/{topic['id']}/messages/{message['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 409


async def test_topic_not_owned(client: AsyncClient):
    token_a = await login(client, "test@example.com", "devpassword123")
    chatgroup = await _create_chatgroup(client, token_a)
    topic = await _create_topic(client, token_a, chatgroup_id=chatgroup["id"])

    email_b, password_b = await _create_other_user(client, token_a)
    token_b = await login(client, email_b, password_b)

    response = await client.get(
        f"/api/v1/topics/{topic['id']}",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert response.status_code == 403
