from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, Keyword, PhoneModel
from app.services.market_research.yandex_direct import DemandService, YandexKeywordStat


class FakeYandexClient:
    def __init__(self, stats):
        self.stats = stats

    def get_stats(self, phrases, region):
        return self.stats


def test_demand_service_saves_stats(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        model = PhoneModel(brand="TestBrand", model_name="X1")
        session.add(model)
        session.flush()
        keyword = Keyword(phrase="дисплей testbrand x1", phone_model_id=model.id)
        session.add(keyword)
        session.commit()

        stat = YandexKeywordStat(
            phrase="дисплей testbrand x1",
            region="225",
            impressions=123,
            clicks=10,
            stat_date=date(2024, 1, 1),
        )
        client = FakeYandexClient(stats=[stat])
        service = DemandService(db=session, client=client, batch_size=10)

        saved = service.update_demand_for_keywords([keyword], region="225")
        session.commit()

        assert len(saved) == 1
        demand = saved[0]
        assert demand.keyword_id == keyword.id
        assert demand.impressions == 123
        assert demand.clicks == 10
        assert demand.date == date(2024, 1, 1)
