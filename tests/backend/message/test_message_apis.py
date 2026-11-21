import uuid

from backend.auth.schemas import Token
from backend.database import get_async_sessionmaker
from backend.message.models import MessageDO
from httpx import AsyncClient

_DONE_STATE = {"event": "done", "topic": "done", "fact": "done", "last_failure_at": None, "retry_count": 0}


async def _seed_derived_data(message_id: str) -> None:
    """Give a message its full set of derived data: an embedding, a salience score and
    every consolidation flag done."""
    async with get_async_sessionmaker()() as session, session.begin():
        row = await session.get(MessageDO, uuid.UUID(message_id))
        assert row is not None
        row.embedding = [0.0] * 768
        row.salience_score = 0.5
        row.processing_state = dict(_DONE_STATE)


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


async def _create_message(
    client: AsyncClient, token: str, chat_id: uuid.UUID, full_text: str, role: str = "user"
) -> dict:
    response = await client.post(
        f"/api/v1/chats/{chat_id}/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={"full_text": full_text, "role": role},
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


async def test_create_message(client: AsyncClient):
    token = await login(client, "test@example.com", "devpassword123")
    chatgroup = await _create_chatgroup(client, token)
    chat = await _create_chat(client, token, chatgroup_id=chatgroup["id"])

    response = await client.post(
        f"/api/v1/chats/{chat['id']}/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={"full_text": "Hello world", "role": "user"},
    )
    assert response.status_code == 201
    data = response.json()
    assert data["full_text"] == "Hello world"
    assert data["role"] == "user"
    assert data["chat_id"] == chat["id"]
    assert data["prev_message_id"] is None
    # Creation writes the text, so edited_at starts at the creation time.
    assert data["edited_at"] == data["created_at"]


async def test_create_message_links_to_last(client: AsyncClient):
    token = await login(client, "test@example.com", "devpassword123")
    chatgroup = await _create_chatgroup(client, token)
    chat = await _create_chat(client, token, chatgroup_id=chatgroup["id"])
    first = await _create_message(client, token, chat_id=chat["id"], full_text="First message")

    response = await client.post(
        f"/api/v1/chats/{chat['id']}/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={"full_text": "Second message", "role": "assistant"},
    )
    assert response.status_code == 201
    data = response.json()
    assert data["prev_message_id"] == first["id"]


async def test_update_message_text_change_requeues_derived_data(client: AsyncClient):
    """Replacing the text records the write in edited_at and requeues every derived
    artifact: the NULL columns requeue the embedding and salience stages, the flags
    requeue consolidation, so the message is rebuilt from scratch by the batch worker."""
    token = await login(client, "test@example.com", "devpassword123")
    chatgroup = await _create_chatgroup(client, token)
    chat = await _create_chat(client, token, chatgroup_id=chatgroup["id"])
    message = await _create_message(client, token, chat_id=chat["id"], full_text="Original text")
    await _seed_derived_data(message["id"])

    response = await client.patch(
        f"/api/v1/chats/{chat['id']}/messages/{message['id']}",
        headers={"Authorization": f"Bearer {token}"},
        json={"full_text": "Updated text"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["full_text"] == "Updated text"
    assert data["prev_message_id"] == message["prev_message_id"]
    assert data["edited_at"] is not None
    assert data["edited_at"] != data["created_at"]  # rewritten after creation

    async with get_async_sessionmaker()() as session:
        row = await session.get(MessageDO, uuid.UUID(message["id"]))
        assert row is not None
        assert row.edited_at is not None
        assert row.embedding is None
        assert row.salience_score is None
        assert row.processing_state["event"] == "waiting"
        assert row.processing_state["topic"] == "waiting"
        assert row.processing_state["fact"] == "waiting"


async def test_update_message_role_only_keeps_derived_data(client: AsyncClient):
    """A role-only change relabels the message without touching the content: edited_at
    keeps its creation value, and the derived data (scored with the old role) is kept
    as is."""
    token = await login(client, "test@example.com", "devpassword123")
    chatgroup = await _create_chatgroup(client, token)
    chat = await _create_chat(client, token, chatgroup_id=chatgroup["id"])
    message = await _create_message(client, token, chat_id=chat["id"], full_text="Original text")
    await _seed_derived_data(message["id"])

    response = await client.patch(
        f"/api/v1/chats/{chat['id']}/messages/{message['id']}",
        headers={"Authorization": f"Bearer {token}"},
        json={"role": "assistant"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["role"] == "assistant"
    assert data["full_text"] == "Original text"
    # The text was not rewritten: edited_at keeps its creation value.
    assert data["edited_at"] == data["created_at"]

    async with get_async_sessionmaker()() as session:
        row = await session.get(MessageDO, uuid.UUID(message["id"]))
        assert row is not None
        assert row.edited_at == row.created_at
        assert row.embedding is not None
        assert row.salience_score == 0.5
        assert row.processing_state["event"] == "done"
        assert row.processing_state["topic"] == "done"
        assert row.processing_state["fact"] == "done"


async def test_update_message_identical_text_bumps_edited_at_without_requeue(client: AsyncClient):
    """Patching the same text still rewrites the field, so edited_at is bumped — but
    the content is unchanged, so nothing derived is requeued."""
    token = await login(client, "test@example.com", "devpassword123")
    chatgroup = await _create_chatgroup(client, token)
    chat = await _create_chat(client, token, chatgroup_id=chatgroup["id"])
    message = await _create_message(client, token, chat_id=chat["id"], full_text="Original text")
    await _seed_derived_data(message["id"])

    response = await client.patch(
        f"/api/v1/chats/{chat['id']}/messages/{message['id']}",
        headers={"Authorization": f"Bearer {token}"},
        json={"full_text": "Original text"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["edited_at"] is not None
    assert data["edited_at"] != data["created_at"]  # the field was rewritten

    async with get_async_sessionmaker()() as session:
        row = await session.get(MessageDO, uuid.UUID(message["id"]))
        assert row is not None
        assert row.edited_at != row.created_at
        # Identical content: the derived data still matches the text.
        assert row.embedding is not None
        assert row.salience_score == 0.5
        assert row.processing_state["event"] == "done"


async def test_retrieve_message(client: AsyncClient):
    token = await login(client, "test@example.com", "devpassword123")
    chatgroup = await _create_chatgroup(client, token)
    chat = await _create_chat(client, token, chatgroup_id=chatgroup["id"])
    message = await _create_message(client, token, chat_id=chat["id"], full_text="Retrieve me")

    response = await client.get(
        f"/api/v1/chats/{chat['id']}/messages/{message['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json() == message


async def test_delete_message(client: AsyncClient):
    token = await login(client, "test@example.com", "devpassword123")
    chatgroup = await _create_chatgroup(client, token)
    chat = await _create_chat(client, token, chatgroup_id=chatgroup["id"])
    message = await _create_message(client, token, chat_id=chat["id"], full_text="Delete me")

    response = await client.delete(
        f"/api/v1/chats/{chat['id']}/messages/{message['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 204


async def test_list_messages(client: AsyncClient):
    token = await login(client, "test@example.com", "devpassword123")
    chatgroup = await _create_chatgroup(client, token)
    chat = await _create_chat(client, token, chatgroup_id=chatgroup["id"])
    [await _create_message(client, token, chat_id=chat["id"], full_text=f"Message {i}") for i in range(3)]

    response = await client.get(
        f"/api/v1/chats/{chat['id']}/messages",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert len(response.json()["items"]) == 3


async def test_message_not_owned(client: AsyncClient):
    token_a = await login(client, "test@example.com", "devpassword123")
    chatgroup = await _create_chatgroup(client, token_a)
    chat = await _create_chat(client, token_a, chatgroup_id=chatgroup["id"])
    message = await _create_message(client, token_a, chat_id=chat["id"], full_text="Owned by A")

    email_b, password_b = await _create_other_user(client, token_a)
    token_b = await login(client, email_b, password_b)

    response = await client.get(
        f"/api/v1/chats/{chat['id']}/messages/{message['id']}",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert response.status_code == 403
