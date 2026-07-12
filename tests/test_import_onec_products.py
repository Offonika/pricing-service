from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.models import Base, PhoneModel, Product, ProductCompatibility, ProductPhoneModel
from tasks.report_product_compatibility_sync import build_report
from tasks.sync_onec_product_catalog import import_onec_products


def test_deprecated_catalog_module_reexports_current_importer() -> None:
    from tasks import import_topcontrol_products_db as deprecated
    from tasks import sync_onec_product_catalog as current

    assert deprecated.import_onec_products is current.import_onec_products


def _setup_onec_schema(engine) -> None:
    statements = [
        """
        CREATE TABLE _Reference62 (
            _IDRRef TEXT PRIMARY KEY,
            _Fld836 TEXT,
            _Description TEXT,
            _ParentIDRRef TEXT,
            _Code TEXT,
            _Fld9175 TEXT,
            _Fld857RRef TEXT,
            _Marked INTEGER,
            _Folder INTEGER
        )
        """,
        """
        CREATE TABLE _Reference26 (
            _IDRRef TEXT PRIMARY KEY,
            _Description TEXT
        )
        """,
        """
        CREATE TABLE _Reference42 (
            _IDRRef TEXT PRIMARY KEY,
            _Description TEXT
        )
        """,
        """
        CREATE TABLE _Chrc401 (
            _IDRRef TEXT PRIMARY KEY,
            _Code TEXT,
            _Description TEXT
        )
        """,
        """
        CREATE TABLE _InfoRg6309 (
            _Fld6310_RRRef TEXT,
            _Fld6311RRef TEXT,
            _Fld6312_S TEXT,
            _Fld6312_RRRef TEXT
        )
        """,
        """
        CREATE TABLE _InfoRg8928 (
            _Fld8929RRef TEXT,
            _Fld8930 TEXT,
            _Fld8934 TEXT,
            _Fld8931 TEXT
        )
        """,
    ]
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


def test_import_onec_products_reads_extended_properties() -> None:
    app_engine = create_engine("sqlite:///:memory:")
    onec_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)
    _setup_onec_schema(onec_engine)

    with onec_engine.begin() as conn:
        conn.execute(text("""
                INSERT INTO _Reference62 (
                    _IDRRef, _Fld836, _Description, _ParentIDRRef, _Code, _Fld9175, _Fld857RRef, _Marked, _Folder
                ) VALUES
                    ('root-1', NULL, 'ОБЩИЙ КАТАЛОГ', NULL, 'ROOT', NULL, NULL, 0, 0),
                    ('parent-1', 'CATROOT', 'Аккумуляторы', 'root-1', 'PARENT', NULL, NULL, 0, 0),
                    ('item-1', '10001', 'Аккумулятор F5 iPhone 11', 'parent-1', 'C1', 'IS1', 'kind-1', 0, 1)
                """))
        conn.execute(text("""
                INSERT INTO _Reference26 (_IDRRef, _Description) VALUES
                    ('kind-1', 'Питание и зарядка (розница + сервис)')
                """))
        conn.execute(text("""
                INSERT INTO _Chrc401 (_IDRRef, _Code, _Description) VALUES
                    ('prop-subject', 'РБ0000100', 'Предмет')
                """))
        conn.execute(text("""
                INSERT INTO _Reference42 (_IDRRef, _Description) VALUES
                    ('subject-1', 'Аккумулятор')
                """))
        conn.execute(text("""
                INSERT INTO _InfoRg6309 (_Fld6310_RRRef, _Fld6311RRef, _Fld6312_S, _Fld6312_RRRef) VALUES
                    ('item-1', 'prop-subject', '', 'subject-1')
                """))
        conn.execute(text("""
                INSERT INTO _InfoRg8928 (_Fld8929RRef, _Fld8930, _Fld8934, _Fld8931) VALUES
                    ('item-1', 'SKU', 'F5-BAT-IPH11-HC3470', '2026-03-11'),
                    ('item-1', 'Емкость', '3470 mAh', '2026-03-11'),
                    ('item-1', 'Повышенная емкость', 'Да', '2026-03-11'),
                    ('item-1', 'Напряжение', '3.87V', '2026-03-11'),
                    ('item-1', 'Wh', '13.39', '2026-03-11'),
                    ('item-1', 'Категория', 'Аккумуляторы', '2026-03-11'),
                    ('item-1', 'Совместим с моделью', 'Apple iPhone 11', '2026-03-11')
                """))

    result = import_onec_products(app_engine, onec_engine)

    assert result["created"] == 1
    with Session(app_engine) as session:
        product = session.query(Product).filter_by(article="10001").one()
        assert product.battery_capacity_mah == 3470
        assert product.battery_is_high_capacity is True
        assert product.battery_voltage == "3.87V"
        assert product.battery_energy_wh == "13.39"
        assert product.subject_1c == "Аккумулятор"
        assert product.subject_generated is None
        assert product.subject == "Аккумулятор"
        assert product.subject_source == "1c"
        assert product.vid_nomenklatury_1c == "Питание и зарядка (розница + сервис)"
        assert product.vid_nomenklatury_generated is None
        assert product.vid_nomenklatury == "Питание и зарядка (розница + сервис)"
        assert product.vid_nomenklatury_source == "1c"
        assert product.category == "Аккумуляторы"
        assert product.fact_sku == "F5-BAT-IPH11-HC3470"
        assert product.code_1c == "C1"
        assert product.info_system_code == "IS1"


def test_import_onec_products_keeps_raw_and_normalized_quality() -> None:
    app_engine = create_engine("sqlite:///:memory:")
    onec_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)
    _setup_onec_schema(onec_engine)

    with onec_engine.begin() as conn:
        conn.execute(text("""
                INSERT INTO _Reference62 (
                    _IDRRef, _Fld836, _Description, _ParentIDRRef, _Code, _Fld9175, _Fld857RRef, _Marked, _Folder
                ) VALUES
                    ('root-1', NULL, 'ОБЩИЙ КАТАЛОГ', NULL, 'ROOT', NULL, NULL, 0, 0),
                    ('parent-1', 'CATROOT', 'Дисплеи', 'root-1', 'PARENT', NULL, NULL, 0, 0),
                    ('item-1', '20001', 'Дисплей для Apple iPhone 11 + тачскрин', 'parent-1', 'C2', 'IS2', 'kind-1', 0, 1)
            """))
        conn.execute(text("""
                INSERT INTO _Reference26 (_IDRRef, _Description) VALUES
                    ('kind-1', 'Дисплеи/сенсор/стекло')
            """))
        conn.execute(text("""
                INSERT INTO _Chrc401 (_IDRRef, _Code, _Description) VALUES
                    ('prop-subject', 'РБ0000100', 'Предмет')
            """))
        conn.execute(text("""
                INSERT INTO _Reference42 (_IDRRef, _Description) VALUES
                    ('subject-1', 'дисплей')
            """))
        conn.execute(text("""
                INSERT INTO _InfoRg6309 (_Fld6310_RRRef, _Fld6311RRef, _Fld6312_S, _Fld6312_RRRef) VALUES
                    ('item-1', 'prop-subject', '', 'subject-1')
            """))
        conn.execute(text("""
                INSERT INTO _InfoRg8928 (_Fld8929RRef, _Fld8930, _Fld8934, _Fld8931) VALUES
                    ('item-1', 'Качество', 'Medium', '2026-03-12'),
                    ('item-1', 'Тип дисплея', 'In-Cell', '2026-03-12'),
                    ('item-1', 'Цвет', 'черный', '2026-03-12')
            """))

    import_onec_products(app_engine, onec_engine)

    with Session(app_engine) as session:
        product = session.query(Product).filter_by(article="20001").one()
        assert product.subject_1c == "дисплей"
        assert product.subject == "дисплей"
        assert product.subject_source == "1c"
        assert product.vid_nomenklatury_1c == "Дисплеи/сенсор/стекло"
        assert product.vid_nomenklatury == "Дисплеи/сенсор/стекло"
        assert product.vid_nomenklatury_source == "1c"
        assert product.quality_raw == "Medium"
        assert product.display_quality_raw is None
        assert product.quality == "Copy Medium"
        assert product.display_quality == "Copy Medium"


def test_import_onec_products_reads_multivalue_compatibility_from_object_properties() -> None:
    app_engine = create_engine("sqlite:///:memory:")
    onec_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)
    _setup_onec_schema(onec_engine)

    with onec_engine.begin() as conn:
        conn.execute(text("""
                INSERT INTO _Reference62 (
                    _IDRRef, _Fld836, _Description, _ParentIDRRef, _Code, _Fld9175, _Fld857RRef, _Marked, _Folder
                ) VALUES
                    ('root-1', NULL, 'ОБЩИЙ КАТАЛОГ', NULL, 'ROOT', NULL, NULL, 0, 0),
                    ('parent-1', 'CATROOT', 'Аккумуляторы', 'root-1', 'PARENT', NULL, NULL, 0, 0),
                    ('item-1', '041567', 'Аккумулятор для Huawei Wi-Fi роутера E5573 / E5577 (HB434666RBC)', 'parent-1', 'C1', 'IS1', 'kind-1', 0, 1)
            """))
        conn.execute(text("""
                INSERT INTO _Reference26 (_IDRRef, _Description) VALUES
                    ('kind-1', 'Питание и зарядка (розница + сервис)')
            """))
        conn.execute(text("""
                INSERT INTO _Chrc401 (_IDRRef, _Code, _Description) VALUES
                    ('prop-brand', 'РБ0000085', 'Совместим с брендом'),
                    ('prop-model', 'РБ0000086', 'Совместим с моделью')
            """))
        conn.execute(text("""
                INSERT INTO _Reference42 (_IDRRef, _Description) VALUES
                    ('brand-huawei', 'Huawei'),
                    ('model-e5573', 'Huawei E5573'),
                    ('model-e5577', 'Huawei E5577')
            """))
        conn.execute(text("""
                INSERT INTO _InfoRg6309 (_Fld6310_RRRef, _Fld6311RRef, _Fld6312_S, _Fld6312_RRRef) VALUES
                    ('item-1', 'prop-brand', '', 'brand-huawei'),
                    ('item-1', 'prop-model', '', 'model-e5573'),
                    ('item-1', 'prop-model', '', 'model-e5577')
            """))
        conn.execute(text("""
                INSERT INTO _InfoRg8928 (_Fld8929RRef, _Fld8930, _Fld8934, _Fld8931) VALUES
                    ('item-1', 'Совместим с моделью', 'Huawei E5577', '2021-12-17')
            """))

    import_onec_products(app_engine, onec_engine)

    with Session(app_engine) as session:
        product = session.query(Product).filter_by(article="041567").one()
        raw_values = {
            row.value
            for row in session.query(ProductCompatibility).filter_by(product_id=product.id)
        }
        assert raw_values == {"Huawei E5573", "Huawei E5577"}
        linked_models = {
            (link.phone_model.brand, link.phone_model.model_name)
            for link in session.query(ProductPhoneModel).filter_by(product_id=product.id)
        }
        assert linked_models == {("huawei", "e5573"), ("huawei", "e5577")}


def test_import_onec_products_uses_compatibility_brand_hint_for_model_without_brand() -> None:
    app_engine = create_engine("sqlite:///:memory:")
    onec_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)
    _setup_onec_schema(onec_engine)

    with onec_engine.begin() as conn:
        conn.execute(text("""
                INSERT INTO _Reference62 (
                    _IDRRef, _Fld836, _Description, _ParentIDRRef, _Code, _Fld9175, _Fld857RRef, _Marked, _Folder
                ) VALUES
                    ('root-1', NULL, 'ОБЩИЙ КАТАЛОГ', NULL, 'ROOT', NULL, NULL, 0, 0),
                    ('parent-1', 'CATROOT', 'Аккумуляторы', 'root-1', 'PARENT', NULL, NULL, 0, 0),
                    ('item-1', '041568', 'Аккумулятор для Wi-Fi роутера E5573', 'parent-1', 'C1', 'IS1', 'kind-1', 0, 1)
            """))
        conn.execute(text("""
                INSERT INTO _Reference26 (_IDRRef, _Description) VALUES
                    ('kind-1', 'Питание и зарядка (розница + сервис)')
            """))
        conn.execute(text("""
                INSERT INTO _Chrc401 (_IDRRef, _Code, _Description) VALUES
                    ('prop-brand', 'РБ0000085', 'Совместим с брендом'),
                    ('prop-model', 'РБ0000086', 'Совместим с моделью')
            """))
        conn.execute(text("""
                INSERT INTO _Reference42 (_IDRRef, _Description) VALUES
                    ('brand-huawei', 'Huawei'),
                    ('model-e5573', 'E5573')
            """))
        conn.execute(text("""
                INSERT INTO _InfoRg6309 (_Fld6310_RRRef, _Fld6311RRef, _Fld6312_S, _Fld6312_RRRef) VALUES
                    ('item-1', 'prop-brand', '', 'brand-huawei'),
                    ('item-1', 'prop-model', '', 'model-e5573')
            """))

    import_onec_products(app_engine, onec_engine)

    with Session(app_engine) as session:
        product = session.query(Product).filter_by(article="041568").one()
        assert {row.value for row in product.compatibilities} == {"E5573"}
        model = session.query(PhoneModel).one()
        assert (model.brand, model.model_name) == ("huawei", "e5573")


def test_import_onec_products_filters_to_general_catalog_branch() -> None:
    app_engine = create_engine("sqlite:///:memory:")
    onec_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)
    _setup_onec_schema(onec_engine)

    with onec_engine.begin() as conn:
        conn.execute(text("""
                INSERT INTO _Reference62 (
                    _IDRRef, _Fld836, _Description, _ParentIDRRef, _Code, _Fld9175, _Fld857RRef, _Marked, _Folder
                ) VALUES
                    ('root-1', NULL, 'ОБЩИЙ КАТАЛОГ', NULL, 'ROOT', NULL, NULL, 0, 0),
                    ('branch-1', 'CATROOT1', 'Дисплеи', 'root-1', 'PARENT1', NULL, NULL, 0, 0),
                    ('item-1', '30001', 'Дисплей для Apple iPhone 11', 'branch-1', 'C3', 'IS3', 'kind-1', 0, 1),
                    ('other-root', NULL, 'РАЗНОЕ', NULL, 'ROOT2', NULL, NULL, 0, 0),
                    ('other-branch', 'CATROOT2', 'Тестовая ветка', 'other-root', 'PARENT2', NULL, NULL, 0, 0),
                    ('item-2', '30002', 'Товар вне общего каталога', 'other-branch', 'C4', 'IS4', 'kind-1', 0, 1)
                """))
        conn.execute(text("""
                INSERT INTO _Reference26 (_IDRRef, _Description) VALUES
                    ('kind-1', 'Дисплеи/сенсор/стекло')
                """))
        conn.execute(text("""
                INSERT INTO _InfoRg8928 (_Fld8929RRef, _Fld8930, _Fld8934, _Fld8931) VALUES
                    ('item-1', 'Цвет', 'черный', '2026-03-12'),
                    ('item-2', 'Цвет', 'красный', '2026-03-12')
                """))

    with Session(app_engine) as session:
        session.add_all(
            [
                Product(
                    article="30002",
                    name="Старый товар вне общего каталога",
                    code_1c="C4",
                    is_active=True,
                ),
                Product(
                    article="30003",
                    name="Старый товар без кода вне общего каталога",
                    planned_sku="OEM-BAT-OLD-1000",
                    is_active=True,
                ),
            ]
        )
        session.commit()

    result = import_onec_products(app_engine, onec_engine)

    assert result["rows"] == 1
    assert result["created"] == 1
    assert result["deactivated_out_of_scope"] == 2
    with Session(app_engine) as session:
        imported = session.query(Product).filter_by(article="30001").one()
        assert imported.name == "Дисплей для Apple iPhone 11"
        assert imported.color == "черный"
        out_of_scope = session.query(Product).filter_by(article="30002").one()
        assert out_of_scope.is_active is False
        out_of_scope_without_code = session.query(Product).filter_by(article="30003").one()
        assert out_of_scope_without_code.is_active is False


def test_product_compatibility_sync_report_is_dry_run() -> None:
    app_engine = create_engine("sqlite:///:memory:")
    onec_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(app_engine)
    _setup_onec_schema(onec_engine)

    with onec_engine.begin() as conn:
        conn.execute(text("""
                INSERT INTO _Reference62 (
                    _IDRRef, _Fld836, _Description, _ParentIDRRef, _Code, _Fld9175, _Fld857RRef, _Marked, _Folder
                ) VALUES
                    ('root-1', NULL, 'ОБЩИЙ КАТАЛОГ', NULL, 'ROOT', NULL, NULL, 0, 0),
                    ('parent-1', 'CATROOT', 'Аккумуляторы', 'root-1', 'PARENT', NULL, NULL, 0, 0),
                    ('item-1', '041567', 'Аккумулятор для Huawei E5573 / E5577', 'parent-1', 'C1', 'IS1', 'kind-1', 0, 1)
            """))
        conn.execute(text("""
                INSERT INTO _Chrc401 (_IDRRef, _Code, _Description) VALUES
                    ('prop-model', 'РБ0000086', 'Совместим с моделью')
            """))
        conn.execute(text("""
                INSERT INTO _Reference42 (_IDRRef, _Description) VALUES
                    ('model-e5573', 'Huawei E5573'),
                    ('model-e5577', 'Huawei E5577')
            """))
        conn.execute(text("""
                INSERT INTO _InfoRg6309 (_Fld6310_RRRef, _Fld6311RRef, _Fld6312_S, _Fld6312_RRRef) VALUES
                    ('item-1', 'prop-model', '', 'model-e5573'),
                    ('item-1', 'prop-model', '', 'model-e5577')
            """))

    with Session(app_engine) as session:
        product = Product(article="041567", name="Аккумулятор для Huawei E5577")
        session.add(product)
        session.flush()
        session.add(ProductCompatibility(product=product, value="Huawei E5577", source="onec"))
        session.commit()

    rows = build_report(
        app_engine,
        onec_engine,
        articles={"041567"},
        site_values={"041567": ["Huawei E5573", "Huawei E5577"]},
        only_mismatches=True,
    )

    assert len(rows) == 1
    assert rows[0].article == "041567"
    assert rows[0].missing_in_pricing == ["Huawei E5573"]
    assert rows[0].missing_on_site == []
    assert rows[0].status == "missing_in_pricing"
    with Session(app_engine) as session:
        product = session.query(Product).filter_by(article="041567").one()
        assert {row.value for row in product.compatibilities} == {"Huawei E5577"}
