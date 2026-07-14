from datetime import date, datetime, timedelta

from app.core.config import get_settings
from app.models import ReleaseStatus, SmartphoneRelease, WeeklySmartphoneDigest
from app.services.weekly_buyer_digest import WeeklyBuyerDigestService
from app.workers.weekly_buyer_digest import run_weekly_buyer_digest_job


class FakeLLM:
    def __init__(self, content: str):
        self.content = content
        self.calls = 0
        self.chat = type("Chat", (), {"completions": self})()

    def create(self, **kwargs):
        self.calls += 1
        message = type("Message", (), {"content": self.content})()
        choice = type("Choice", (), {"message": message})()
        return type("Response", (), {"choices": [choice]})()


def _make_release(**kwargs) -> SmartphoneRelease:
    model_slug = kwargs.get("model", "X1")
    defaults = {
        "brand": "Acme",
        "model": "X1",
        "full_name": "Acme X1",
        "announcement_date": date.today(),
        "release_status": ReleaseStatus.ANNOUNCED.value,
        "source_type": "news_api",
        "source_name": "Test",
        "source_url": f"https://example.com/{model_slug}",
        "is_active": True,
        "created_at": datetime.utcnow(),
    }
    defaults.update(kwargs)
    return SmartphoneRelease(**defaults)


def test_weekly_digest_creates_record(db_session):
    today = date(2024, 1, 7)
    week_start = today - timedelta(days=6)
    release1 = _make_release(
        brand="BrandA",
        model="Model1",
        announcement_date=today - timedelta(days=1),
        summary_ru="Короткое описание",
    )
    release2 = _make_release(
        brand="BrandB",
        model="Model2",
        announcement_date=today - timedelta(days=2),
        release_status=ReleaseStatus.RELEASED.value,
        summary_ru="Продажи начались",
    )
    db_session.add_all([release1, release2])
    db_session.commit()

    llm = FakeLLM("LLM digest text")
    service = WeeklyBuyerDigestService(db_session, llm, model="gpt-test")
    result = service.generate_weekly_digest(week_start, today)

    assert result["release_count"] == 2
    assert result["brand_count"] == 2
    assert result["action"] == "created"
    assert llm.calls == 1

    digest = db_session.query(WeeklySmartphoneDigest).first()
    assert digest is not None
    assert digest.content.startswith("LLM digest text")
    assert digest.release_ids == [release1.id, release2.id]


def test_weekly_digest_updates_existing(db_session):
    week_start = date(2024, 2, 5)
    week_end = date(2024, 2, 11)
    existing = WeeklySmartphoneDigest(
        week_start=week_start,
        week_end=week_end,
        content="Old text",
        model="old",
        stats={},
    )
    db_session.add(existing)
    release = _make_release(
        brand="BrandA",
        model="Model1",
        announcement_date=week_start,
        summary_ru="Новый обзор",
    )
    db_session.add(release)
    db_session.commit()

    llm = FakeLLM("Updated digest text")
    service = WeeklyBuyerDigestService(db_session, llm, model="gpt-test")
    result = service.generate_weekly_digest(week_start, week_end)

    assert result["action"] == "updated"
    digest = db_session.query(WeeklySmartphoneDigest).first()
    assert digest.content.startswith("Updated digest text")
    assert digest.model == "gpt-test"


def test_weekly_digest_handles_empty_week(db_session):
    week_start = date(2024, 3, 4)
    week_end = date(2024, 3, 10)
    service = WeeklyBuyerDigestService(db_session, llm_client=None, model="gpt-test")

    result = service.generate_weekly_digest(week_start, week_end)

    assert result["release_count"] == 0
    assert result["llm_used"] is False
    digest = db_session.query(WeeklySmartphoneDigest).first()
    assert "Значимых анонсов" in digest.content


def test_weekly_digest_job_respects_feature_flag(monkeypatch):
    monkeypatch.setenv("WEEKLY_BUYER_DIGEST_ENABLED", "false")
    get_settings.cache_clear()

    result = run_weekly_buyer_digest_job()
    assert result["skipped"] is True

    get_settings.cache_clear()


def test_weekly_digest_job_uses_injected_service(monkeypatch):
    monkeypatch.setenv("WEEKLY_BUYER_DIGEST_ENABLED", "true")
    get_settings.cache_clear()

    class RecordingService:
        def __init__(self):
            self.called = False
            self.args = None

        def generate_weekly_digest(self, week_start, week_end):
            self.called = True
            self.args = (week_start, week_end)
            return {
                "skipped": False,
                "week_start": week_start.isoformat(),
                "week_end": week_end.isoformat(),
                "release_count": 0,
                "brand_count": 0,
                "errors": 0,
                "digest_id": 1,
            }

    service = RecordingService()
    today = date(2024, 1, 7)
    result = run_weekly_buyer_digest_job(service=service, today=today)

    assert result["skipped"] is False
    assert service.called is True
    assert service.args[0] == today - timedelta(days=6)
    assert service.args[1] == today

    get_settings.cache_clear()
