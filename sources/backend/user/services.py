from backend.exceptions import UserNotFoundProblem
from backend.user.schemas import UserCreateInternal, UserInternal
from backend.user.stores import UserStore
from backend.utils.security import verify_password
from pydantic import SecretStr


class UserService:
    def __init__(self) -> None:
        self.store = UserStore

    async def create(self, user: UserCreateInternal) -> UserInternal:
        return await self.store.create(user)

    async def retrieve(self, user_uuid: str) -> UserInternal:
        user = await self.store.retrieve(user_uuid)
        if user is None or not user.is_active:
            raise UserNotFoundProblem(f"User {user_uuid} not found")
        return user

    async def get_by_email(self, email: str) -> UserInternal | None:
        return await self.store.retrieve_by_email(email)

    async def authenticate(self, email: str, password: SecretStr) -> UserInternal | None:
        user = await self.store.retrieve_by_email(email)
        if user is None or not user.is_active:
            return None
        if not verify_password(password, user.hashed_password):
            return None
        return user
