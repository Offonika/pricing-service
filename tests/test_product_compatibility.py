from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, PhoneModel, Product, ProductCompatibility, ProductPhoneModel
from tasks.import_topcontrol_products_db import upsert_product_compatibility


def test_upsert_product_compatibility_adds_and_dedupes():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        p = Product(article="A1", name="Product 1")
        session.add(p)
        session.commit()

        upsert_product_compatibility(session, p, ["Nokia 2630", "Nokia 2630", "Nokia 2760"])
        session.commit()

        values = {c.value for c in session.query(ProductCompatibility).all()}
        assert values == {"Nokia 2630", "Nokia 2760"}
        links = session.query(ProductPhoneModel).all()
        assert len(links) == 2
        models = {(m.brand, m.model_name) for m in session.query(PhoneModel).all()}
        assert models == {("nokia", "2630"), ("nokia", "2760")}

        # update: remove one value, add another
        upsert_product_compatibility(session, p, ["Nokia 2760", "Nokia 5000"])
        session.commit()

        values2 = {c.value for c in session.query(ProductCompatibility).all()}
        assert values2 == {"Nokia 2760", "Nokia 5000"}
        link_models = {
            (link.phone_model.brand, link.phone_model.model_name)
            for link in session.query(ProductPhoneModel).all()
        }
        assert link_models == {("nokia", "2760"), ("nokia", "5000")}


def test_upsert_product_compatibility_keeps_existing_links_when_only_filtered_values_remain():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        p = Product(
            article="A2",
            name="Защитное стекло для Apple iPhone 11",
            subject_1c="защитное стекло",
            vid_nomenklatury_1c="Аксессуары (розничные товары)",
        )
        session.add(p)
        session.commit()

        upsert_product_compatibility(session, p, ["Apple iPhone 11"])
        session.commit()

        initial_links = {
            (link.phone_model.brand, link.phone_model.model_name)
            for link in session.query(ProductPhoneModel).all()
        }
        assert initial_links == {("apple", "iphone 11")}

        upsert_product_compatibility(session, p, ["Apple Watch S8 (41 мм)"])
        session.commit()

        link_models = {
            (link.phone_model.brand, link.phone_model.model_name)
            for link in session.query(ProductPhoneModel).all()
        }
        assert link_models == {("apple", "iphone 11")}
