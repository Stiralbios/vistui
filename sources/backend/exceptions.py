from enum import StrEnum, auto

from starlette import status


class ProblemKind(StrEnum):
    NOT_FOUND = auto()
    ALREADY_EXIST = auto()
    NOT_ALLOWED = auto()


class Entity(StrEnum):
    USER = auto()


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
