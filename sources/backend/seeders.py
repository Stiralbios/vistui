import datetime as dt
import uuid

from backend.chat.models import ChatDO
from backend.chatgroup.models import ChatGroupDO
from backend.chatgroup.schemas import ChatGroupConfig, ChatGroupPrompts
from backend.database import get_async_sessionmaker
from backend.exceptions import UserAlreadyExistProblem
from backend.message.constants import MessageRole, ProcessingStatus
from backend.message.models import MessageDO
from backend.settings import InitSettings
from backend.user.models import UserDO
from backend.user.schemas import UserCreateInternal
from backend.user.services import UserService
from backend.utils.security import hash_password
from loguru import logger
from sqlalchemy import select


async def create_default_user():
    email = InitSettings().DEFAULT_EMAIL
    password = InitSettings().DEFAULT_PASSWORD

    if not email and not password:
        logger.info("No default user credentials provided, skipping creation")
        return
    if not email or not password:
        logger.warning("DEFAULT_EMAIL and DEFAULT_PASSWORD must both be set to create a default user")
        return

    user = UserCreateInternal(
        name="Default User",
        email=email,
        hashed_password=hash_password(password),
        is_active=True,
    )
    try:
        await UserService().create(user)
    except UserAlreadyExistProblem:
        logger.info("Default user already exists, skipping creation")
        return

    logger.info(f"Default user created successfully with email: {email}")


_DEMO_MESSAGES = [
    (MessageRole.USER, "Hi, I'd like to plan a trip to Japan next spring."),
    (MessageRole.ASSISTANT, "That sounds exciting! What cities are you considering?"),
    (MessageRole.USER, "Tokyo and Kyoto for sure, maybe Osaka too."),
    (MessageRole.ASSISTANT, "Great choices. How long are you planning to stay?"),
    (MessageRole.USER, "About two weeks. I also want to visit during cherry blossom season."),
    (MessageRole.ASSISTANT, "Cherry blossoms in late March to early April are beautiful in those areas."),
    (MessageRole.USER, "By the way, I started learning Japanese last month."),
    (MessageRole.ASSISTANT, "Good for you! Consistency is key with language learning."),
    (MessageRole.USER, "I practice 20 minutes every morning with an app."),
    (MessageRole.ASSISTANT, "A small daily habit adds up quickly."),
    (MessageRole.USER, "Work has been busy too. We're launching a new product in Q2."),
    (MessageRole.ASSISTANT, "Sounds intense. Do you have a clear roadmap?"),
    (MessageRole.USER, "Yes, but the deadline moved up by two weeks."),
    (MessageRole.ASSISTANT, "That can be stressful. Make sure to communicate blockers early."),
    (MessageRole.USER, "My mom is doing better after her surgery."),
    (MessageRole.ASSISTANT, "I'm glad to hear that. Recovery takes time."),
    (MessageRole.USER, "She's walking more and even started gardening again."),
    (MessageRole.ASSISTANT, "That's a wonderful sign of progress."),
    (MessageRole.USER, "I need to buy a new laptop soon. Mine is getting too slow."),
    (MessageRole.ASSISTANT, "What will you mainly use it for?"),
    (MessageRole.USER, "Programming, some light video editing, and writing."),
    (MessageRole.ASSISTANT, "A mid-range laptop with 16GB RAM should handle that well."),
    (MessageRole.USER, "I also want to get back into running."),
    (MessageRole.ASSISTANT, "Start slow to avoid injury. Maybe a couch-to-5K plan?"),
    (MessageRole.USER, "I ran 3km yesterday without stopping."),
    (MessageRole.ASSISTANT, "Nice work! Recovery days are just as important."),
    (MessageRole.USER, "I'm thinking of switching to a standing desk."),
    (MessageRole.ASSISTANT, "Standing desks can help with posture if you alternate sitting and standing."),
    (MessageRole.USER, "I should meal prep on Sundays to save time during the week."),
    (MessageRole.ASSISTANT, "Meal prepping is a great time saver and can help you eat healthier."),
]


def _default_processing_state() -> dict:
    return {
        "embedding": ProcessingStatus.WAITING,
        "salience": ProcessingStatus.DONE,
        "event": ProcessingStatus.WAITING,
        "topic": ProcessingStatus.WAITING,
        "fact": ProcessingStatus.WAITING,
        "last_failure_at": None,
        "retry_count": 0,
    }


async def seed_demo_chat() -> dict:
    """Idempotently seed a demo ChatGroup/Chat with 30 messages for Phase 3 testing."""
    settings = InitSettings()
    if not settings.DEFAULT_EMAIL:
        logger.warning("Cannot seed demo chat: DEFAULT_EMAIL is not set")
        return {"chatgroup_id": None, "chat_id": None, "message_count": 0}

    async with get_async_sessionmaker()() as session, session.begin():
        user = await session.scalar(select(UserDO).where(UserDO.email == settings.DEFAULT_EMAIL))
        if user is None:
            logger.warning(f"Cannot seed demo chat: no user with email {settings.DEFAULT_EMAIL}")
            return {"chatgroup_id": None, "chat_id": None, "message_count": 0}

        chatgroup = await session.scalar(
            select(ChatGroupDO).where(ChatGroupDO.user_id == user.id, ChatGroupDO.name == "Phase 3 Demo")
        )
        if chatgroup is None:
            chatgroup = ChatGroupDO(
                id=uuid.uuid4(),
                user_id=user.id,
                name="Phase 3 Demo",
                description="Chat group used for Phase 3 embedding and consolidation testing.",
                config=ChatGroupConfig().model_dump(),
                prompts=ChatGroupPrompts().model_dump(),
                ranking_formula=(
                    "lambda vector_score, bm25_score, salience, linked_count: vector_score * salience * linked_count"
                ),
            )
            session.add(chatgroup)
            await session.flush()

        chat = await session.scalar(
            select(ChatDO).where(ChatDO.chatgroup_id == chatgroup.id, ChatDO.name == "Demo Chat")
        )
        if chat is not None:
            logger.info("Demo chat already exists, skipping seeding")
            return {"chatgroup_id": chatgroup.id, "chat_id": chat.id, "message_count": 0}

        chat = ChatDO(
            id=uuid.uuid4(),
            chatgroup_id=chatgroup.id,
            name="Demo Chat",
            description="Demo chat for Phase 3 testing.",
        )
        session.add(chat)
        await session.flush()

        prev_id: uuid.UUID | None = None
        created_at = dt.datetime.now(dt.timezone.utc)
        for role, text in _DEMO_MESSAGES:
            message = MessageDO(
                id=uuid.uuid4(),
                chat_id=chat.id,
                prev_message_id=prev_id,
                full_text=text,
                role=role,
                processing_state=_default_processing_state(),
                created_at=created_at,
            )
            session.add(message)
            await session.flush()
            prev_id = message.id
            created_at = created_at + dt.timedelta(seconds=1)

        logger.info(f"Seeded demo chat with {len(_DEMO_MESSAGES)} messages")
        return {"chatgroup_id": chatgroup.id, "chat_id": chat.id, "message_count": len(_DEMO_MESSAGES)}
