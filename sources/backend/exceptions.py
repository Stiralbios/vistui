from enum import StrEnum, auto

from starlette import status


class ProblemKind(StrEnum):
    NOT_FOUND = auto()
    ALREADY_EXIST = auto()
    NOT_ALLOWED = auto()


class Entity(StrEnum):
    USER = auto()
    CHATGROUP = auto()
    CHAT = auto()
    MESSAGE = auto()
    EVENT = auto()
    TOPIC = auto()
    FACT = auto()


class BaseProblem(Exception):
    kind: ProblemKind
    status: int
    entity: Entity

    def __init__(self, detail, *args):
        self.detail = detail
        if self.__class__ is BaseProblem:
            raise TypeError("Cannot instantiate abstract class 'BaseProblem' directly")
        super().__init__(*args)

    @property
    def title(self):
        kind = self.kind.title().replace("_", " ")
        return f"{self.entity.title()} {kind}"


class UserNotFoundProblem(BaseProblem):
    kind = ProblemKind.NOT_FOUND
    status = status.HTTP_404_NOT_FOUND
    entity = Entity.USER


class UserAlreadyExistProblem(BaseProblem):
    kind = ProblemKind.ALREADY_EXIST
    entity = Entity.USER
    status = status.HTTP_409_CONFLICT


class UserNotAllowedProblem(BaseProblem):
    kind = ProblemKind.NOT_ALLOWED
    entity = Entity.USER
    status = status.HTTP_403_FORBIDDEN


class ChatGroupNotFoundProblem(BaseProblem):
    kind = ProblemKind.NOT_FOUND
    status = status.HTTP_404_NOT_FOUND
    entity = Entity.CHATGROUP


class ChatGroupAlreadyExistProblem(BaseProblem):
    kind = ProblemKind.ALREADY_EXIST
    status = status.HTTP_409_CONFLICT
    entity = Entity.CHATGROUP


class ChatGroupNotAllowedProblem(BaseProblem):
    kind = ProblemKind.NOT_ALLOWED
    status = status.HTTP_403_FORBIDDEN
    entity = Entity.CHATGROUP


class ChatNotFoundProblem(BaseProblem):
    kind = ProblemKind.NOT_FOUND
    status = status.HTTP_404_NOT_FOUND
    entity = Entity.CHAT


class ChatAlreadyExistProblem(BaseProblem):
    kind = ProblemKind.ALREADY_EXIST
    status = status.HTTP_409_CONFLICT
    entity = Entity.CHAT


class ChatNotAllowedProblem(BaseProblem):
    kind = ProblemKind.NOT_ALLOWED
    status = status.HTTP_403_FORBIDDEN
    entity = Entity.CHAT


class MessageNotFoundProblem(BaseProblem):
    kind = ProblemKind.NOT_FOUND
    status = status.HTTP_404_NOT_FOUND
    entity = Entity.MESSAGE


class MessageAlreadyExistProblem(BaseProblem):
    kind = ProblemKind.ALREADY_EXIST
    status = status.HTTP_409_CONFLICT
    entity = Entity.MESSAGE


class MessageNotAllowedProblem(BaseProblem):
    kind = ProblemKind.NOT_ALLOWED
    status = status.HTTP_403_FORBIDDEN
    entity = Entity.MESSAGE


class EventNotFoundProblem(BaseProblem):
    kind = ProblemKind.NOT_FOUND
    status = status.HTTP_404_NOT_FOUND
    entity = Entity.EVENT


class EventAlreadyExistProblem(BaseProblem):
    kind = ProblemKind.ALREADY_EXIST
    status = status.HTTP_409_CONFLICT
    entity = Entity.EVENT


class EventNotAllowedProblem(BaseProblem):
    kind = ProblemKind.NOT_ALLOWED
    status = status.HTTP_403_FORBIDDEN
    entity = Entity.EVENT


class TopicNotFoundProblem(BaseProblem):
    kind = ProblemKind.NOT_FOUND
    status = status.HTTP_404_NOT_FOUND
    entity = Entity.TOPIC


class TopicAlreadyExistProblem(BaseProblem):
    kind = ProblemKind.ALREADY_EXIST
    status = status.HTTP_409_CONFLICT
    entity = Entity.TOPIC


class TopicNotAllowedProblem(BaseProblem):
    kind = ProblemKind.NOT_ALLOWED
    status = status.HTTP_403_FORBIDDEN
    entity = Entity.TOPIC


class FactNotFoundProblem(BaseProblem):
    kind = ProblemKind.NOT_FOUND
    status = status.HTTP_404_NOT_FOUND
    entity = Entity.FACT


class FactAlreadyExistProblem(BaseProblem):
    kind = ProblemKind.ALREADY_EXIST
    status = status.HTTP_409_CONFLICT
    entity = Entity.FACT


class FactNotAllowedProblem(BaseProblem):
    kind = ProblemKind.NOT_ALLOWED
    status = status.HTTP_403_FORBIDDEN
    entity = Entity.FACT
