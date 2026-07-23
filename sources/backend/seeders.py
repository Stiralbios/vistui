from backend.exceptions import UserAlreadyExistProblem
from backend.settings import InitSettings
from backend.user.schemas import UserCreateInternal
from backend.user.services import UserService
from backend.utils.security import hash_password
from loguru import logger


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
