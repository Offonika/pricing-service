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
        canonical_display_name="МСК-025 Радиорынок «Электромир»",
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
                "МСК-025 Радиорынок «Электромир»",
                "МСК-025 Радиорынок Электромир",
            }
        ),
    ),
    ReceivableDepartmentAliasGroup(
        key="spb_prosveshcheniya",
        canonical_display_name="СПБ-034 Проспект Просвещения",
        refs=frozenset(),
        aliases=frozenset(
            {
                "10. СПБ Просвещения",
                "10.СПБ Просвещения",
                "СПБ Просвещения",
                "СПБ-034 Проспект Просвещения",
                "СПБ 034 Проспект Просвещения",
                "Проспект Просвещения",
            }
        ),
    ),
    ReceivableDepartmentAliasGroup(
        key="grand_yug",
        canonical_display_name="МСК-028 ТЦ Гранд Юг «Электронный рай»",
        refs=frozenset(),
        aliases=frozenset(
            {
                "06. Гранд Юг",
                "06.Гранд Юг",
                "Гранд Юг",
                "МСК-028 ТЦ Гранд Юг «Электронный рай»",
                "МСК-028 ТЦ Гранд Юг Электронный рай",
                "МСК 028 ТЦ Гранд Юг Электронный рай",
            }
        ),
    ),
    ReceivableDepartmentAliasGroup(
        key="spb_sadovaya",
        canonical_display_name="СПБ-029 Садовая",
        refs=frozenset(),
        aliases=frozenset(
            {
                "09. СПБ Садовая",
                "09.СПБ Садовая",
                "СПБ Садовая",
                "СПБ-029 Садовая",
                "СПБ 029 Садовая",
                "Садовая",
            }
        ),
    ),
    ReceivableDepartmentAliasGroup(
        key="elektronika_na_presne",
        canonical_display_name="МСК-027 ТЦ «Электроника на Пресне»",
        refs=frozenset(),
        aliases=frozenset(
            {
                "07. Электроника на пресне",
                "07. Электроника на Пресне",
                "07.Электроника на пресне",
                "Электроника на пресне",
                "Электроника на Пресне",
                "МСК-027 ТЦ «Электроника на Пресне»",
                "МСК-027 ТЦ Электроника на Пресне",
                "МСК 027 ТЦ Электроника на Пресне",
            }
        ),
    ),
    ReceivableDepartmentAliasGroup(
        key="spb_moskovskaya",
        canonical_display_name="СПБ-035 Московская",
        refs=frozenset(),
        aliases=frozenset(
            {
                "13. СПБ Московская",
                "13.СПБ Московская",
                "СПБ Московская",
                "СПБ-035 Московская",
                "СПБ 035 Московская",
                "Московская",
            }
        ),
    ),
    ReceivableDepartmentAliasGroup(
        key="gorbushkin_dvor",
        canonical_display_name="МСК-017 Техномолл «Горбушкин Двор»",
        refs=frozenset(),
        aliases=frozenset(
            {
                "01. Горбушкин Двор",
                "01.Горбушкин Двор",
                "Горбушкин Двор",
                "Горбушка",
                "МСК-017 Техномолл «Горбушкин Двор»",
                "МСК-017 Техномолл Горбушкин Двор",
                "МСК 017 Техномолл Горбушкин Двор",
            }
        ),
    ),
    ReceivableDepartmentAliasGroup(
        key="mitino",
        canonical_display_name="МСК-019 ТК «Митинский радиорынок»",
        refs=frozenset(),
        aliases=frozenset(
            {
                "03. Митино",
                "03.Митино",
                "Митино",
                "МСК-019 ТК «Митинский радиорынок»",
                "МСК-019 ТК Митинский радиорынок",
                "МСК 019 ТК Митинский радиорынок",
                "Митинский радиорынок",
            }
        ),
    ),
    ReceivableDepartmentAliasGroup(
        key="savelovskiy",
        canonical_display_name="МСК-015 ТК «Савеловский» Мобильный",
        refs=frozenset(),
        aliases=frozenset(
            {
                "02. Савеловский",
                "02.Савеловский",
                "Савеловский",
                "Савелово",
                "ТК Савеловский",
                "МСК-015 ТК «Савеловский» Мобильный",
                "МСК-015 ТК Савеловский Мобильный",
                "МСК 015 ТК Савеловский Мобильный",
            }
        ),
    ),
    ReceivableDepartmentAliasGroup(
        key="pyatigorsk",
        canonical_display_name="ПТГ-022 Георгиевская",
        refs=frozenset(),
        aliases=frozenset(
            {
                "05 Пятигорск",
                "05. Пятигорск",
                "05.Пятигорск",
                "5 .Пятигорск",
                "5 .Пятигорск (сотрудники)",
                "Пятигорск",
                "ПТГ-022 Георгиевская",
                "ПТГ 022 Георгиевская",
                "Георгиевская",
            }
        ),
    ),
    ReceivableDepartmentAliasGroup(
        key="shchelkovskaya",
        canonical_display_name="МСК-033 Щелковская",
        refs=frozenset(),
        aliases=frozenset(
            {
                "12. Щелковская",
                "12.Щелковская",
                "Щелковская",
                "МСК-033 Щелковская",
                "МСК 033 Щелковская",
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
