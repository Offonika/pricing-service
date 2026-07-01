from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

TEPLY_STAN_RECEIVABLES_REF = "0x82490025901e48ee11e7ab5f8735cce5"
TEPLY_STAN_STAFF_DEPARTMENT_REF = "0xbe600025901e48ee11eb176f19f074be"
TEPLY_STAN_TELEPHONY_STORE_REF = "0xbb990025901e48ee11e5cb12dbcbb95a"


@dataclass(frozen=True)
class ReceivableDepartmentAliasGroup:
    key: str
    canonical_display_name: str
    refs: frozenset[str]
    aliases: frozenset[str]


RECEIVABLE_DEPARTMENT_ALIAS_GROUPS = (
    ReceivableDepartmentAliasGroup(
        key="online_store",
        canonical_display_name="Онлайн-магазин",
        refs=frozenset(),
        aliases=frozenset(
            {
                "08. Сайт",
                "08.Сайт",
                "Сайт",
                "Онлайн-магазин",
                "Онлайн магазин",
                "online",
            }
        ),
    ),
    ReceivableDepartmentAliasGroup(
        key="teply_stan",
        canonical_display_name="04.Теплый Стан",
        refs=frozenset(
            {
                TEPLY_STAN_RECEIVABLES_REF,
                TEPLY_STAN_STAFF_DEPARTMENT_REF,
                TEPLY_STAN_TELEPHONY_STORE_REF,
            }
        ),
        aliases=frozenset(
            {
                "04.Теплый Стан",
                "04. Теплый Стан",
                "Теплый Стан",
                "Радиорынок «Электромир»",
                "Радиорынок Электромир",
                'Теплый стан Радиорынок "Электромир" пав. 652',
                "МСК-025 Радиорынок Электромир",
            }
        ),
    ),
)


def _normalize_ref(value: object) -> str:
    return str(value or "").strip()


def _ref_key(value: object) -> str:
    return _normalize_ref(value).casefold()


def _text_key(value: object) -> str:
    text = str(value or "").strip().casefold().replace("ё", "е")
    text = text.replace("«", "").replace("»", "").replace('"', "").replace("'", "")
    return re.sub(r"[\s._-]+", " ", text).strip()


_ALIAS_GROUP_BY_REF = {
    _ref_key(ref): group for group in RECEIVABLE_DEPARTMENT_ALIAS_GROUPS for ref in group.refs
}
_ALIAS_GROUP_BY_NAME = {
    _text_key(alias): group
    for group in RECEIVABLE_DEPARTMENT_ALIAS_GROUPS
    for alias in group.aliases
}


def receivable_department_alias_key(value: object) -> str | None:
    group = _ALIAS_GROUP_BY_NAME.get(_text_key(value))
    return group.key if group else None


def receivable_department_display_name(value: str | None) -> str | None:
    group = _ALIAS_GROUP_BY_NAME.get(_text_key(value))
    if group is not None:
        return group.canonical_display_name
    return value


def receivable_department_names_equivalent(left: str | None, right: str | None) -> bool:
    left_group = _ALIAS_GROUP_BY_NAME.get(_text_key(left))
    right_group = _ALIAS_GROUP_BY_NAME.get(_text_key(right))
    if left_group is not None and right_group is not None:
        return left_group.key == right_group.key
    return bool(left and right and _text_key(left) == _text_key(right))


def expand_receivable_department_refs(
    refs: Iterable[object] | None = None,
    *,
    names: Iterable[object] | None = None,
) -> frozenset[str]:
    expanded: set[str] = {
        normalized for normalized in (_normalize_ref(ref) for ref in refs or []) if normalized
    }
    for ref in list(expanded):
        group = _ALIAS_GROUP_BY_REF.get(_ref_key(ref))
        if group is not None:
            expanded.update(group.refs)
    for name in names or []:
        group = _ALIAS_GROUP_BY_NAME.get(_text_key(name))
        if group is not None:
            expanded.update(group.refs)
    return frozenset(expanded)
