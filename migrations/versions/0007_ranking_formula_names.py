"""ChatGroups reference ranking formulas by name

`chatgroup.ranking_formula` stores the name of an entry of the settings file's
`ranking_formulas` section instead of the lambda source itself: the API accepts
names only, so no user-authored string is ever evaluated. Rows holding the
built-in default formula text become "default"; any other stored formula cannot
be mapped automatically and aborts the migration — register the formula in the
settings file's ranking_formulas section under a name first, set the chatgroup to
that name, then re-run.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-21

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: Union[str, Sequence[str], None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DEFAULT_FORMULA_NAME = "default"
_LEGACY_DEFAULT_FORMULA = (
    "lambda vector_score, bm25_score, salience, linked_count: "
    "max(vector_score, bm25_score) * salience * link_weight(linked_count)"
)


def _unmigratable(where: str, params: dict) -> list[tuple]:
    return op.get_bind().execute(sa.text(f"SELECT id, name FROM chatgroup WHERE {where}"), params).fetchall()


def upgrade() -> None:
    """Rewrite the built-in default formula source into its registry name."""
    unknown = _unmigratable(
        "ranking_formula != :legacy AND ranking_formula != :name",
        {"legacy": _LEGACY_DEFAULT_FORMULA, "name": _DEFAULT_FORMULA_NAME},
    )
    if unknown:
        listed = ", ".join(f"{name} ({chatgroup_id})" for chatgroup_id, name in unknown)
        raise RuntimeError(
            "chatgroups with custom ranking formulas cannot be migrated automatically: "
            f"{listed}. Register each formula in the settings file's ranking_formulas "
            "section, set the chatgroups' ranking_formula to the registered names, "
            "then re-run the migration."
        )
    op.get_bind().execute(sa.text("UPDATE chatgroup SET ranking_formula = :name"), {"name": _DEFAULT_FORMULA_NAME})


def downgrade() -> None:
    """Restore the built-in default formula source for chatgroups referencing it by
    name; chatgroups referencing other names cannot be downgraded (their source is
    gone) and abort."""
    unknown = _unmigratable(
        "ranking_formula != :name AND ranking_formula != :legacy",
        {"name": _DEFAULT_FORMULA_NAME, "legacy": _LEGACY_DEFAULT_FORMULA},
    )
    if unknown:
        listed = ", ".join(f"{name} ({chatgroup_id})" for chatgroup_id, name in unknown)
        raise RuntimeError(
            "chatgroups referencing named (non-default) ranking formulas cannot be "
            f"downgraded automatically: {listed}. The lambda source is not stored "
            "anywhere once formulas are referenced by name."
        )
    op.get_bind().execute(
        sa.text("UPDATE chatgroup SET ranking_formula = :legacy"), {"legacy": _LEGACY_DEFAULT_FORMULA}
    )
