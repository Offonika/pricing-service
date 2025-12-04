from datetime import date, datetime

from app.core.config import get_settings
from app.models import ReleaseStatus, SmartphoneRelease, PhoneModel, Keyword
from app.services.smartphone_releases import (
    NormalizedReleaseCandidate,
    RawNewsItem,
    SmartphoneReleaseService,
)
from app.services.smartphone_releases.service import HIGHLIGHT_LIMIT
from app.workers.smartphone_releases import run_smartphone_release_job
from app.workers.smartphone_releases import sync_releases_to_phone_models


class FakeNewsClient:
    def __init__(self, items):
        self.items = items

    def fetch_recent_news(self):
        return list(self.items)


class FakeNormalizer:
    def __init__(self, result):
        self.result = result

    def normalize(self, _item):
        return self.result


class RecordingExtraSource(FakeNewsClient):
    def __init__(self, items):
        super().__init__(items)
        self.called = False

    def fetch_recent_news(self):
        self.called = True
        return super().fetch_recent_news()


class PerItemNormalizer:
    def normalize(self, item):
        meta = item.raw.get("meta", {})
        return NormalizedReleaseCandidate(
            is_phone_announcement=True,
            brand=meta.get("brand", "UnknownBrand"),
            model=meta.get("model", "UnknownModel"),
            models=meta.get("models"),
            announcement_date=date(2025, 1, 1),
            release_status="announced",
            market_release_date=None,
            market_release_date_ru=None,
            summary_ru=f"{meta.get('brand', 'UnknownBrand')} {meta.get('model', 'UnknownModel')} анонсирован",
        )


def test_smartphone_release_service_creates_and_updates(db_session):
    raw_item = RawNewsItem(
        title="Acme launches X1",
        description="New smartphone launch",
        url="https://example.com/x1",
        published_at=datetime.utcnow(),
        source_name="NewsAPI",
        raw={"id": 1},
    )
    initial_normalized = NormalizedReleaseCandidate(
        is_phone_announcement=True,
        brand="Acme",
        model="X1",
        models=["X1"],
        announcement_date=date(2025, 1, 10),
        release_status="announced",
        market_release_date=date(2025, 2, 1),
        market_release_date_ru=None,
        summary_ru="Новый смартфон Acme X1 анонсирован",
    )
    service = SmartphoneReleaseService(
        db=db_session,
        news_client=FakeNewsClient([raw_item]),
        normalizer=FakeNormalizer(initial_normalized),
    )

    result = service.ingest_latest()
    assert result["created"] == 1
    release = db_session.query(SmartphoneRelease).first()
    assert release is not None
    assert release.brand == "Acme"
    assert release.model == "X1"
    assert release.release_status == ReleaseStatus.ANNOUNCED
    assert release.market_release_date == date(2025, 2, 1)
    assert release.summary_ru == "Новый смартфон Acme X1 анонсирован"

    updated_normalized = NormalizedReleaseCandidate(
        is_phone_announcement=True,
        brand="Acme",
        model="X1",
        models=["X1"],
        announcement_date=date(2025, 1, 10),
        release_status="released",
        market_release_date=date(2025, 2, 5),
        market_release_date_ru=date(2025, 3, 1),
        summary_ru="Acme X1 поступил в продажу",
    )
    second_service = SmartphoneReleaseService(
        db=db_session,
        news_client=FakeNewsClient([raw_item]),
        normalizer=FakeNormalizer(updated_normalized),
    )
    updated_result = second_service.ingest_latest()
    assert updated_result["updated"] == 1
    updated_release = db_session.query(SmartphoneRelease).first()
    assert updated_release.release_status == ReleaseStatus.RELEASED


def test_service_merges_api_and_extra_sources(db_session):
    api_item = RawNewsItem(
        title="NewsCo Alpha launch",
        description="API feed item",
        url="https://example.com/api-alpha",
        published_at=datetime.utcnow(),
        source_name="NewsAPI",
        raw={"meta": {"brand": "NewsCo", "models": ["Alpha", "Alpha Plus"]}},
    )
    rss_item = RawNewsItem(
        title="RSSCo Beta launch",
        description="RSS feed item",
        url="https://example.com/rss-beta",
        published_at=datetime.utcnow(),
        source_name="GSMArena",
        raw={"meta": {"brand": "RSSCo", "models": ["Beta"]}},
    )
    news_client = FakeNewsClient([api_item])
    extra_source = RecordingExtraSource([rss_item])
    service = SmartphoneReleaseService(
        db=db_session,
        news_client=news_client,
        normalizer=PerItemNormalizer(),
        extra_sources=[extra_source],
    )

    result = service.ingest_latest()

    assert result["created"] == 3
    assert extra_source.called is True
    releases = db_session.query(SmartphoneRelease).all()
    assert {release.brand for release in releases} == {"NewsCo", "RSSCo"}
    assert sorted([release.model for release in releases if release.brand == "NewsCo"]) == ["Alpha", "Alpha Plus"]


def test_service_returns_highlights(db_session):
    raw_item = RawNewsItem(
        title="Acme launches Z1",
        description="Compact flagship announced",
        url="https://example.com/z1",
        published_at=datetime.utcnow(),
        source_name="NewsAPI",
        raw={"meta": {"brand": "Acme", "model": "Z1"}},
    )
    service = SmartphoneReleaseService(
        db=db_session,
        news_client=FakeNewsClient([raw_item]),
        normalizer=PerItemNormalizer(),
    )

    result = service.ingest_latest()

    assert result["created"] == 1
    assert result["highlights"]
    highlight = result["highlights"][0]
    assert highlight["title"] == "Acme launches Z1"
    assert "Acme" in highlight["summary"]


def test_service_limits_highlights(db_session):
    items = []
    for idx in range(HIGHLIGHT_LIMIT + 3):
        items.append(
            RawNewsItem(
                title=f"Brand{idx} Model{idx} launch",
                description=f"Launch for Brand{idx} Model{idx}",
                url=f"https://example.com/{idx}",
                published_at=datetime.utcnow(),
                source_name="NewsAPI",
                raw={"meta": {"brand": f"Brand{idx}", "model": f"Model{idx}"}},
            )
        )
    service = SmartphoneReleaseService(
        db=db_session,
        news_client=FakeNewsClient(items),
        normalizer=PerItemNormalizer(),
    )

    result = service.ingest_latest()

    assert result["created"] == len(items)
    assert len(result["highlights"]) == HIGHLIGHT_LIMIT


def test_service_splits_models_from_single_field(db_session):
    raw_item = RawNewsItem(
        title="Split launch",
        description="Several models announced",
        url="https://example.com/split",
        published_at=datetime.utcnow(),
        source_name="NewsAPI",
        raw={"meta": {"brand": "SplitCo", "model": "R1, R1 Pro and R1 Ultra"}},
    )
    service = SmartphoneReleaseService(
        db=db_session,
        news_client=FakeNewsClient([raw_item]),
        normalizer=PerItemNormalizer(),
    )

    result = service.ingest_latest()

    assert result["created"] == 3
    models = sorted([release.model for release in db_session.query(SmartphoneRelease).filter_by(brand="SplitCo").all()])
    assert models == ["R1", "R1 Pro", "R1 Ultra"]


def test_job_respects_feature_flag(monkeypatch):
    monkeypatch.setenv("SMARTPHONE_RELEASES_ENABLED", "false")
    get_settings.cache_clear()

    class StubService:
        def ingest_latest(self):
            return {}

    result = run_smartphone_release_job(service=StubService())
    assert result["skipped"] is True

    get_settings.cache_clear()


def test_job_uses_injected_service(monkeypatch):
    monkeypatch.setenv("SMARTPHONE_RELEASES_ENABLED", "true")
    get_settings.cache_clear()

    class RecordingService:
        def __init__(self):
            self.called = False

        def ingest_latest(self):
            self.called = True
            return {"created": 0, "updated": 0, "processed": 0, "errors": 0, "fetched": 0, "skipped": 0}

    service = RecordingService()
    result = run_smartphone_release_job(service=service)
    assert result["skipped"] is False
    assert service.called is True

    get_settings.cache_clear()


def test_sync_releases_to_phone_models(db_session):
    release = SmartphoneRelease(
        brand="SyncBrand",
        model="SyncModel",
        full_name="SyncBrand SyncModel",
        announcement_date=date(2025, 1, 1),
        release_status="announced",
        source_type="news_api",
        source_name="TestSource",
        source_url="https://example.com/sync",
        is_active=True,
    )
    db_session.add(release)
    db_session.commit()

    stats = sync_releases_to_phone_models(db_session)
    assert stats["synced_models"] == 1
    model = db_session.query(PhoneModel).filter_by(brand="SyncBrand", model_name="SyncModel").first()
    assert model is not None
    keywords = db_session.query(Keyword).filter_by(phone_model_id=model.id).all()
    assert keywords  # keywords generated
