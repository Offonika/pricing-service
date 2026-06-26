from __future__ import annotations

from datetime import date
from html import escape
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.api.dependencies import get_db, require_site_defect_archive_internal_token
from app.core.config import get_settings
from app.schemas.site_defect_archive import (
    SiteDefectArchiveCaseDetailResponse,
    SiteDefectArchiveSearchResponse,
    SiteDefectWorkingAnalyzeRequest,
    SiteDefectWorkingAnalyzeResponse,
    SiteDefectWorkingReportResponse,
)
from app.services.site_defect_archive import (
    PROBLEM_TYPE_LABELS,
    SiteDefectArchiveFilters,
    get_archive_case,
    search_archive_cases,
)
from app.services.site_defect_workflow import (
    analyze_working_reclamation_text,
    build_working_reclamations_report,
)

router = APIRouter(dependencies=[Depends(require_site_defect_archive_internal_token)])
page_router = APIRouter()


@router.get("/archive", response_model=SiteDefectArchiveSearchResponse)
def search_archive(
    q: str | None = Query(default=None, description="Поиск по текстам, номерам и файлам"),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    author: str | None = Query(default=None),
    problem_type: str | None = Query(default=None),
    number: str | None = Query(default=None),
    has_file: bool | None = Query(default=None),
    has_photo: bool | None = Query(default=None),
    has_video: bool | None = Query(default=None),
    has_linked_expertise: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    filters = SiteDefectArchiveFilters(
        query=q,
        date_from=date_from,
        date_to=date_to,
        author=author,
        problem_type=problem_type,
        number=number,
        has_file=has_file,
        has_photo=has_photo,
        has_video=has_video,
        has_linked_expertise=has_linked_expertise,
        limit=limit,
        offset=offset,
    )
    items, total = search_archive_cases(db, filters)
    return {"total": total, "limit": limit, "offset": offset, "items": items}


@router.get("/archive/{case_id}", response_model=SiteDefectArchiveCaseDetailResponse)
def get_archive_detail(case_id: int, db: Session = Depends(get_db)):
    item = get_archive_case(db, case_id=case_id)
    if item is None:
        raise HTTPException(status_code=404, detail="archive case not found")
    return item


@router.post("/working/analyze", response_model=SiteDefectWorkingAnalyzeResponse)
def analyze_working_reclamation(
    payload: SiteDefectWorkingAnalyzeRequest,
    db: Session = Depends(get_db),
):
    return analyze_working_reclamation_text(
        db,
        title=payload.title,
        customer_contact=payload.customer_contact,
        order_refs=payload.order_refs,
        product_model=payload.product_model,
        problem_description=payload.problem_description,
        customer_request=payload.customer_request,
        comments_text=payload.comments_text,
        similar_limit=payload.similar_limit,
    )


@router.get("/working/report", response_model=SiteDefectWorkingReportResponse)
def working_reclamations_report(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    return build_working_reclamations_report(db, limit=limit)


@page_router.get("/site-defects/archive", response_class=HTMLResponse, include_in_schema=False)
def site_defect_archive_page(
    q: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    author: str | None = None,
    problem_type: str | None = None,
    number: str | None = None,
    has_file: bool | None = None,
    has_photo: bool | None = None,
    has_video: bool | None = None,
    has_linked_expertise: bool | None = None,
    token: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    settings = get_settings()
    expected_token = (
        settings.site_defect_archive_internal_api_token
        or settings.expertise_internal_api_token
        or settings.management_internal_api_token
    )
    if expected_token and token != expected_token:
        return HTMLResponse(_render_locked_page())

    filters = SiteDefectArchiveFilters(
        query=q,
        date_from=date_from,
        date_to=date_to,
        author=author,
        problem_type=problem_type,
        number=number,
        has_file=has_file,
        has_photo=has_photo,
        has_video=has_video,
        has_linked_expertise=has_linked_expertise,
        limit=50,
        offset=0,
    )
    items, total = search_archive_cases(db, filters)
    return HTMLResponse(
        _render_archive_page(
            items=items,
            total=total,
            filters=filters,
            token=token if expected_token else None,
        )
    )


def _render_locked_page() -> str:
    return """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Поиск по архиву браков сайта</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, sans-serif; margin: 32px; color: #1f2933; }
    form { display: flex; gap: 8px; max-width: 520px; }
    input { flex: 1; padding: 10px 12px; border: 1px solid #cbd5e1; border-radius: 6px; }
    button { padding: 10px 14px; border: 0; border-radius: 6px; background: #1d4ed8; color: white; }
  </style>
</head>
<body>
  <h1>Поиск по архиву браков сайта</h1>
  <p>Для просмотра архива нужен внутренний токен доступа.</p>
  <form method="get" action="/site-defects/archive">
    <input type="password" name="token" placeholder="Внутренний токен">
    <button type="submit">Открыть</button>
  </form>
</body>
</html>"""


def _render_archive_page(
    *,
    items: list[dict],
    total: int,
    filters: SiteDefectArchiveFilters,
    token: str | None,
) -> str:
    hidden_token = (
        f'<input type="hidden" name="token" value="{escape(token)}">' if token else ""
    )
    problem_options = "\n".join(
        f'<option value="{escape(key)}"{" selected" if filters.problem_type == key else ""}>'
        f"{escape(label)}</option>"
        for key, label in PROBLEM_TYPE_LABELS.items()
    )
    rows = "\n".join(_render_result(item) for item in items)
    if not rows:
        rows = '<div class="empty">Ничего не найдено.</div>'
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Поиск по архиву браков сайта</title>
  <style>
    :root {{ color-scheme: light; }}
    body {{ margin: 0; font-family: system-ui, -apple-system, Segoe UI, sans-serif; background: #f6f7f9; color: #1f2933; }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 24px; }}
    h1 {{ margin: 0 0 18px; font-size: 26px; letter-spacing: 0; }}
    form {{ display: grid; grid-template-columns: 2fr repeat(3, minmax(140px, 1fr)); gap: 10px; margin-bottom: 16px; }}
    input, select {{ width: 100%; box-sizing: border-box; padding: 9px 10px; border: 1px solid #cbd5e1; border-radius: 6px; background: white; color: #1f2933; }}
    button {{ padding: 9px 14px; border: 0; border-radius: 6px; background: #14532d; color: white; font-weight: 600; cursor: pointer; }}
    .checks {{ display: flex; flex-wrap: wrap; gap: 12px; align-items: center; grid-column: 1 / -2; }}
    .checks label {{ display: inline-flex; align-items: center; gap: 6px; font-size: 14px; }}
    .actions {{ display: flex; justify-content: flex-end; }}
    .count {{ margin: 8px 0 14px; color: #475569; }}
    .result {{ background: white; border: 1px solid #d8dee8; border-radius: 8px; padding: 14px 16px; margin-bottom: 10px; }}
    .meta {{ display: flex; flex-wrap: wrap; gap: 8px; color: #64748b; font-size: 13px; margin-bottom: 6px; }}
    .title {{ font-size: 18px; font-weight: 700; margin-bottom: 6px; }}
    .summary {{ line-height: 1.45; margin-bottom: 8px; }}
    .numbers {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }}
    .tag {{ background: #eef2ff; color: #3730a3; border-radius: 5px; padding: 3px 7px; font-size: 12px; }}
    .snippet {{ color: #334155; font-size: 14px; margin: 4px 0; }}
    .links {{ display: flex; gap: 8px; margin-top: 10px; }}
    .links a {{ color: #0f766e; text-decoration: none; font-weight: 600; }}
    .empty {{ background: white; border: 1px solid #d8dee8; border-radius: 8px; padding: 20px; }}
    @media (max-width: 760px) {{
      main {{ padding: 16px; }}
      form {{ grid-template-columns: 1fr; }}
      .checks, .actions {{ grid-column: auto; justify-content: stretch; }}
      button {{ width: 100%; }}
    }}
  </style>
</head>
<body>
<main>
  <h1>Поиск по архиву браков сайта</h1>
  <form method="get" action="/site-defects/archive">
    {hidden_token}
    <input name="q" placeholder="Текст, номер, файл" value="{escape(filters.query or '')}">
    <input name="number" placeholder="Заказ / РБГУ / перемещение" value="{escape(filters.number or '')}">
    <input name="author" placeholder="Автор" value="{escape(filters.author or '')}">
    <select name="problem_type">
      <option value="">Любой тип</option>
      {problem_options}
    </select>
    <input type="date" name="date_from" value="{filters.date_from.isoformat() if filters.date_from else ''}">
    <input type="date" name="date_to" value="{filters.date_to.isoformat() if filters.date_to else ''}">
    <div class="checks">
      {_checkbox("has_file", "есть файлы", filters.has_file)}
      {_checkbox("has_photo", "есть фото", filters.has_photo)}
      {_checkbox("has_video", "есть видео", filters.has_video)}
      {_checkbox("has_linked_expertise", "есть экспертиза", filters.has_linked_expertise)}
    </div>
    <div class="actions"><button type="submit">Найти</button></div>
  </form>
  <div class="count">Найдено: {total}</div>
  {rows}
</main>
</body>
</html>"""


def _checkbox(name: str, label: str, value: bool | None) -> str:
    checked = " checked" if value is True else ""
    return f'<label><input type="checkbox" name="{escape(name)}" value="true"{checked}> {escape(label)}</label>'


def _render_result(item: dict) -> str:
    numbers = "".join(
        f'<span class="tag">{escape(str(number))}</span>'
        for number in item.get("extracted_numbers", [])[:12]
    )
    snippets = "".join(
        f'<div class="snippet">{escape(snippet)}</div>' for snippet in item.get("snippets", [])[:3]
    )
    links = []
    if item.get("bitrix_detail_url"):
        links.append(f'<a href="{escape(item["bitrix_detail_url"])}" target="_blank">Открыть карточку Bitrix</a>')
    if item.get("bitrix_disk_folder_url"):
        links.append(f'<a href="{escape(item["bitrix_disk_folder_url"])}" target="_blank">Открыть файлы</a>')
    query = urlencode({"q": item.get("source_post_message_id") or ""})
    links.append(f'<a href="/api/site-defects/archive/{int(item["id"])}?{query}">JSON</a>')
    links_html = f'<div class="links">{"".join(links)}</div>' if links else ""
    meta = [
        item.get("posted_at") or "без даты",
        item.get("author_name") or "автор не указан",
        item.get("problem_type_label") or item.get("problem_type") or "тип не указан",
        f'{item.get("comment_count", 0)} комм.',
        f'{item.get("file_count", 0)} файл.',
    ]
    return f"""<article class="result">
  <div class="meta">{' · '.join(escape(str(part)) for part in meta)}</div>
  <div class="title">{escape(item.get("title") or "")}</div>
  <div class="summary">{escape(item.get("summary") or "")}</div>
  <div class="numbers">{numbers}</div>
  {snippets}
  {links_html}
</article>"""
