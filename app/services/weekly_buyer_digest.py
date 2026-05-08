from __future__ import annotations

import base64
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from openai import OpenAI
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import ReleaseStatus, SmartphoneRelease, WeeklySmartphoneDigest

DIGEST_SYSTEM_PROMPT = """
Ты аналитик для отдела закупок сети Master Mobile.
Подготовь развёрнутый, но лаконичный обзор новинок смартфонов за неделю.
Ответ верни в Markdown с чёткой структурой:
- краткое резюме недели (2–4 пункта);
- разбор по брендам/ценовым сегментам с выводами;
- акценты на важных моделях и рисках/возможностях;
- рекомендации закупщикам: на что обратить внимание,
  какие линейки и запчасти готовить, что мониторить по ценам/наличию.
Избегай воды и рекламных лозунгов, пиши по делу для закупщиков.
"""
PREVIEW_LIMIT = 700
SUMMARY_LIMIT = 260
DEFAULT_MAX_ITEMS = 120
DIGEST_OUTPUT_DIR = Path("data/digests/weekly")
DIGEST_IMAGE_SIZE = (1200, 630)
DIGEST_IMAGE_BG = "#0b1221"
DIGEST_IMAGE_FG = "#f7f8fb"
DIGEST_IMAGE_ACCENT = "#4fc3f7"
DIGEST_IMAGE_SECONDARY = "#c9d3e1"
DIGEST_IMAGE_PADDING = 56
DIGEST_IMAGE_HL_LIMIT = 140
DIGEST_IMAGE_TEASER_LIMIT = 180
DIGEST_IMAGE_FALLBACK_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _status_label(status: str | None) -> str | None:
    if not status:
        return None
    mapping = {
        ReleaseStatus.RUMOR.value: "слух",
        ReleaseStatus.ANNOUNCED.value: "анонс",
        ReleaseStatus.RELEASED.value: "в продаже",
    }
    if isinstance(status, ReleaseStatus):
        normalized = status.value
    else:
        normalized = str(status).lower().strip()
    return mapping.get(normalized, normalized)


def _shorten(text: str, limit: int) -> str:
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


@dataclass
class ReleaseSnapshot:
    id: int
    brand: str
    model: str
    status: str | None
    announcement_date: date | None
    release_date: date | None
    summary: str | None

    @property
    def status_label(self) -> str | None:
        return _status_label(self.status)

    def short_line(self) -> str:
        bits = [self.model]
        if self.status_label:
            bits.append(f"({self.status_label})")
        date_part = self.release_date or self.announcement_date
        if date_part:
            bits.append(str(date_part))
        tail = " ".join(bits).strip()
        summary = _shorten(self.summary or "", 140) if self.summary else ""
        if summary:
            return f"{tail} — {summary}"
        return tail


class WeeklySmartphoneDigestRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_period(self, week_start: date, week_end: date) -> WeeklySmartphoneDigest | None:
        return (
            self.db.query(WeeklySmartphoneDigest)
            .filter(
                WeeklySmartphoneDigest.week_start == week_start,
                WeeklySmartphoneDigest.week_end == week_end,
            )
            .first()
        )

    def create(self, payload: dict) -> WeeklySmartphoneDigest:
        digest = WeeklySmartphoneDigest(**payload)
        self.db.add(digest)
        self.db.flush()
        return digest

    def update(self, digest: WeeklySmartphoneDigest, payload: dict) -> WeeklySmartphoneDigest:
        for key, value in payload.items():
            setattr(digest, key, value)
        self.db.add(digest)
        self.db.flush()
        return digest


class WeeklyBuyerDigestService:
    def __init__(self, db: Session, llm_client: OpenAI | None, model: str | None) -> None:
        self.db = db
        self.repo = WeeklySmartphoneDigestRepository(db)
        self.llm_client = llm_client
        self.model = model
        self.logger = logging.getLogger("app.services.weekly_buyer_digest")

    def generate_weekly_digest(self, week_start: date, week_end: date) -> dict[str, object]:
        releases = self._load_releases(week_start, week_end)
        release_snapshots = [self._to_snapshot(item) for item in releases]
        brand_counts = self._brand_counts(release_snapshots)
        prompt_body = self._build_prompt(week_start, week_end, release_snapshots, brand_counts)
        prompt_chars = len(prompt_body.encode("utf-8"))
        content, llm_used = self._render_digest(
            prompt_body, release_snapshots, week_start, week_end
        )

        stats = {
            "release_count": len(release_snapshots),
            "brand_count": len(brand_counts),
            "brand_counts": brand_counts,
        }
        payload = {
            "week_start": week_start,
            "week_end": week_end,
            "content": content,
            "model": self.model,
            "prompt": prompt_body,
            "prompt_chars": prompt_chars,
            "release_ids": [item.id for item in release_snapshots],
            "stats": stats,
        }

        digest = self.repo.get_by_period(week_start, week_end)
        action = "updated" if digest else "created"
        if digest:
            digest = self.repo.update(digest, payload)
        else:
            digest = self.repo.create(payload)
        self.db.commit()

        overview = self._build_overview(release_snapshots)
        preview = self._make_preview(content)
        artifacts = self._save_artifacts(
            content=content,
            overview=overview,
            preview=preview,
            week_start=week_start,
            week_end=week_end,
            stats=stats,
        )
        return {
            "skipped": False,
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "release_count": len(release_snapshots),
            "brand_count": len(brand_counts),
            "brands": list(brand_counts.keys()),
            "digest_id": digest.id,
            "action": action,
            "model": self.model,
            "prompt_chars": prompt_chars,
            "preview": preview,
            "overview": overview,
            "llm_used": llm_used,
            "errors": 0,
            **artifacts,
        }

    def _load_releases(self, week_start: date, week_end: date) -> list[SmartphoneRelease]:
        def _between(column):
            return and_(column.isnot(None), column >= week_start, column <= week_end)

        query = (
            self.db.query(SmartphoneRelease)
            .filter(
                SmartphoneRelease.is_active.is_(True),
                SmartphoneRelease.brand.isnot(None),
                SmartphoneRelease.model.isnot(None),
                SmartphoneRelease.release_status.in_(
                    [
                        ReleaseStatus.ANNOUNCED.value,
                        ReleaseStatus.RELEASED.value,
                        ReleaseStatus.ANNOUNCED,
                        ReleaseStatus.RELEASED,
                    ]
                ),
                or_(
                    _between(SmartphoneRelease.announcement_date),
                    _between(SmartphoneRelease.market_release_date),
                    # Фолбэк: если нет дат, используем дату создания записи.
                    and_(
                        SmartphoneRelease.announcement_date.is_(None),
                        SmartphoneRelease.market_release_date.is_(None),
                        SmartphoneRelease.created_at.between(week_start, week_end),
                    ),
                ),
            )
            .order_by(
                SmartphoneRelease.announcement_date.desc(),
                SmartphoneRelease.market_release_date.desc(),
                SmartphoneRelease.created_at.desc(),
            )
        )
        return query.limit(DEFAULT_MAX_ITEMS).all()

    def _to_snapshot(self, release: SmartphoneRelease) -> ReleaseSnapshot:
        return ReleaseSnapshot(
            id=release.id,
            brand=release.brand.strip(),
            model=release.model.strip(),
            status=release.release_status,
            announcement_date=release.announcement_date,
            release_date=release.market_release_date,
            summary=release.summary_ru or release.summary,
        )

    def _brand_counts(self, releases: Sequence[ReleaseSnapshot]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in releases:
            key = item.brand
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: kv[0]))

    def _build_prompt(
        self,
        week_start: date,
        week_end: date,
        releases: Sequence[ReleaseSnapshot],
        brand_counts: dict[str, int],
    ) -> str:
        if not releases:
            return (
                f"Период: {week_start} — {week_end}\n"
                "За последние 7 дней не найдено новых анонсов или релизов смартфонов."
            )

        grouped: dict[str, list[ReleaseSnapshot]] = {}
        for item in releases:
            grouped.setdefault(item.brand, []).append(item)
        for brand in grouped:
            grouped[brand].sort(
                key=lambda x: (x.release_date or x.announcement_date or week_end), reverse=True
            )

        brand_lines: list[str] = []
        for brand, items in grouped.items():
            brand_lines.append(f"{brand} ({brand_counts.get(brand, 0)}):")
            for snap in items:
                status = snap.status_label or "статус не указан"
                date_part = snap.release_date or snap.announcement_date
                meta_bits = [status]
                if date_part:
                    meta_bits.append(str(date_part))
                summary = _shorten(snap.summary or "", SUMMARY_LIMIT)
                description = summary or "без краткого описания"
                brand_lines.append(f"- {snap.model} ({', '.join(meta_bits)}) — {description}")

        header = (
            f"Период: {week_start} — {week_end}\n"
            f"Всего релизов: {len(releases)}, брендов: {len(brand_counts)}\n"
            "Список новинок:\n"
        )
        return header + "\n".join(brand_lines)

    def _render_digest(
        self,
        prompt_body: str,
        releases: Sequence[ReleaseSnapshot],
        week_start: date,
        week_end: date,
    ) -> tuple[str, bool]:
        if not releases:
            return self._empty_digest(week_start, week_end), False
        if not self.llm_client or not self.model:
            return self._fallback_digest(prompt_body, releases, week_start, week_end), False

        try:
            response = self.llm_client.chat.completions.create(
                model=self.model,
                temperature=0.2,
                max_completion_tokens=900,
                messages=[
                    {"role": "system", "content": DIGEST_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt_body},
                ],
            )
            content = (response.choices[0].message.content or "").strip()
            if content:
                return content, True
            self.logger.warning("LLM returned empty content; falling back to template")
            return self._fallback_digest(prompt_body, releases, week_start, week_end), False
        except Exception:
            self.logger.exception("failed to generate weekly digest via LLM")
            return self._fallback_digest(prompt_body, releases, week_start, week_end), False

    def _fallback_digest(
        self,
        prompt_body: str,
        releases: Sequence[ReleaseSnapshot],
        week_start: date,
        week_end: date,
    ) -> str:
        lines = [
            "Еженедельный обзор новинок смартфонов для закупщиков Master Mobile",
            f"Период: {week_start} — {week_end}",
            "",
            "LLM недоступна, собран краткий список вручную:",
            prompt_body,
        ]
        return "\n".join(lines)

    def _empty_digest(self, week_start: date, week_end: date) -> str:
        return (
            "Еженедельный обзор новинок смартфонов для закупщиков Master Mobile\n"
            f"Период: {week_start} — {week_end}\n"
            "- Значимых анонсов или релизов за неделю не найдено. "
            "Продолжаем мониторинг и обновим обзор при появлении новинок."
        )

    def _build_overview(self, releases: Sequence[ReleaseSnapshot], limit: int = 5) -> list[str]:
        lines: list[str] = []
        for item in releases[:limit]:
            lines.append(f"{item.brand} {item.short_line()}")
        return lines

    def _make_preview(self, content: str) -> str:
        if not content:
            return ""
        return _shorten(content, PREVIEW_LIMIT)

    def _save_artifacts(
        self,
        content: str,
        overview: Sequence[str],
        preview: str,
        week_start: date,
        week_end: date,
        stats: dict[str, object],
    ) -> dict[str, str | None]:
        output_dir = DIGEST_OUTPUT_DIR
        output_dir.mkdir(parents=True, exist_ok=True)

        highlight = overview[0] if overview else preview
        md_name = f"{week_end.isoformat()}.md"
        md_path = output_dir / md_name
        md_path.write_text(content, encoding="utf-8")

        png_name = f"{week_end.isoformat()}.png"
        png_path = output_dir / png_name
        png_saved = self._render_image(
            target=png_path,
            overview=overview,
            preview=preview,
            week_start=week_start,
            week_end=week_end,
            stats=stats,
        )

        cover_path = self._generate_cover_image(
            highlight_text=highlight or "",
            week_end=week_end,
            output_dir=output_dir,
        )

        return {
            "markdown_path": str(md_path),
            "image_path": str(png_path if png_saved else ""),
            "cover_path": str(cover_path) if cover_path else "",
        }

    def _render_image(
        self,
        target: Path,
        overview: Sequence[str],
        preview: str,
        week_start: date,
        week_end: date,
        stats: dict[str, object],
    ) -> bool:
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:  # pragma: no cover - Pillow может отсутствовать в окружении
            self.logger.warning("Pillow is not installed; skipping weekly digest image generation")
            return False

        def _clean(text: str) -> str:
            cleaned = (text or "").replace("**", "").replace("*", "")
            cleaned = cleaned.replace("# ", "").replace("## ", "").replace("### ", "")
            cleaned = cleaned.replace("`", "").strip()
            return cleaned

        highlight = ""
        if overview:
            highlight = _shorten(_clean(str(overview[0])), DIGEST_IMAGE_HL_LIMIT)
        elif preview:
            highlight = _shorten(_clean(preview), DIGEST_IMAGE_HL_LIMIT)
        else:
            highlight = "Новые анонсы и релизы за неделю"

        teaser_source = overview[1] if len(overview) > 1 else ""
        teaser = (
            _shorten(_clean(str(teaser_source)), DIGEST_IMAGE_TEASER_LIMIT) if teaser_source else ""
        )

        total_releases = stats.get("release_count", "?")
        brand_count = stats.get("brand_count", "?")
        metric_line = f"Релизов: {total_releases} · Брендов: {brand_count}"
        period_line = f"{week_start} — {week_end}"
        top_items = [_clean(str(item)) for item in overview[1:4]]

        def _load_font(path: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                return ImageFont.load_default()

        title_font = _load_font(DIGEST_IMAGE_FALLBACK_FONT, 54)
        subtitle_font = _load_font(DIGEST_IMAGE_FALLBACK_FONT, 32)
        body_font = _load_font(DIGEST_IMAGE_FALLBACK_FONT, 30)
        metric_font = _load_font(DIGEST_IMAGE_FALLBACK_FONT, 26)

        img = Image.new("RGB", DIGEST_IMAGE_SIZE, DIGEST_IMAGE_BG)
        draw = ImageDraw.Draw(img)

        def _text_height(text: str, font) -> int:
            _, _, _, h = draw.textbbox((0, 0), text or " ", font=font)
            return h

        def _wrap(text: str, font, max_width: int) -> list[str]:
            if not text:
                return []
            lines: list[str] = []
            for paragraph in text.splitlines():
                words = paragraph.split()
                if not words:
                    lines.append("")
                    continue
                current = ""
                for word in words:
                    candidate = f"{current} {word}".strip()
                    if draw.textlength(candidate, font=font) <= max_width:
                        current = candidate
                    else:
                        if current:
                            lines.append(current)
                        current = word
                if current:
                    lines.append(current)
            return lines

        x0, y0 = DIGEST_IMAGE_PADDING, DIGEST_IMAGE_PADDING
        max_width = DIGEST_IMAGE_SIZE[0] - 2 * DIGEST_IMAGE_PADDING

        draw.text((x0, y0), "Еженедельный обзор новинок", font=title_font, fill=DIGEST_IMAGE_FG)
        y0 += _text_height("Hg", title_font) + 20

        draw.text((x0, y0), period_line, font=subtitle_font, fill=DIGEST_IMAGE_SECONDARY)
        y0 += _text_height("Hg", subtitle_font) + 26

        for line in _wrap(highlight, body_font, max_width):
            draw.text((x0, y0), line, font=body_font, fill=DIGEST_IMAGE_ACCENT)
            y0 += _text_height(line, body_font) + 8

        if teaser:
            y0 += 8
            for line in _wrap(teaser, body_font, max_width):
                draw.text((x0, y0), line, font=body_font, fill=DIGEST_IMAGE_FG)
                y0 += _text_height(line, body_font) + 6

        if top_items:
            y0 += 18
            draw.text(
                (x0, y0), "Топ новинок недели:", font=subtitle_font, fill=DIGEST_IMAGE_SECONDARY
            )
            y0 += _text_height("Hg", subtitle_font) + 10
            bullet_indent = 24
            for item in top_items:
                bullet = f"• {item}"
                for line in _wrap(bullet, body_font, max_width - bullet_indent):
                    draw.text((x0 + bullet_indent, y0), line, font=body_font, fill=DIGEST_IMAGE_FG)
                    y0 += _text_height(line, body_font) + 6

        y0 += 16
        draw.text((x0, y0), metric_line, font=metric_font, fill=DIGEST_IMAGE_SECONDARY)

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            img.save(target, format="PNG")
            return True
        except Exception:
            self.logger.exception("failed to save weekly digest image")
            return False

    def _generate_cover_image(
        self, highlight_text: str, week_end: date, output_dir: Path
    ) -> Path | None:
        if not self.llm_client:
            return None
        prompt = (
            "Editorial photo-style illustration for a weekly smartphone release digest. "
            "Show a modern smartphone on a clean background with subtle lighting, "
            "no text on the image, focus on the device. "
            f"Theme: {highlight_text or 'new smartphone announcements and releases this week'}."
        )
        try:
            response = self.llm_client.images.generate(
                model="gpt-image-1",
                prompt=prompt,
                size="1024x1024",
                quality="high",
            )
            if not response.data:
                self.logger.warning("Image API returned no data")
                return None
            b64 = response.data[0].b64_json
            if not b64:
                self.logger.warning("Image API returned empty payload")
                return None
            cover_bytes = base64.b64decode(b64)
            target = output_dir / f"{week_end.isoformat()}-cover.png"
            target.write_bytes(cover_bytes)
            return target
        except Exception:
            self.logger.exception("failed to generate cover image via OpenAI Images API")
            return None


def build_weekly_buyer_digest_service(db: Session) -> WeeklyBuyerDigestService:
    settings = get_settings()
    client: OpenAI | None = None
    if settings.openai_api_key:
        client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_api_base)
    else:
        logging.getLogger("app.services.weekly_buyer_digest").warning(
            "OPENAI_API_KEY is not set; weekly digest will use fallback text"
        )
    model_name = settings.weekly_buyer_digest_model or settings.openai_model
    return WeeklyBuyerDigestService(db=db, llm_client=client, model=model_name)
