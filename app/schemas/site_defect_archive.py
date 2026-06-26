from __future__ import annotations

from pydantic import BaseModel, Field


class SiteDefectArchiveFileResponse(BaseModel):
    id: int
    source_file_id: str | None = None
    source_message_id: str | None = None
    name: str
    storage_path: str | None = None
    content_type: str | None = None
    extension: str | None = None
    size: int | None = None
    bitrix_disk_file_id: str | None = None
    bitrix_disk_url: str | None = None


class SiteDefectArchiveMessageResponse(BaseModel):
    id: int
    source_message_id: str
    message_kind: str
    message_at: str | None = None
    author_name: str | None = None
    text: str
    file_ids: list[str] = Field(default_factory=list)


class SiteDefectArchiveCaseListItem(BaseModel):
    id: int
    idempotency_key: str
    source_dialog_id: str
    source_post_message_id: str
    source_comment_chat_id: str | None = None
    posted_at: str | None = None
    author_name: str | None = None
    title: str
    summary: str | None = None
    problem_type: str
    problem_type_label: str
    status: str
    extracted_numbers: list[str] = Field(default_factory=list)
    comment_count: int
    file_count: int
    has_photo: bool
    has_video: bool
    bitrix_entity_id: str | None = None
    bitrix_detail_url: str | None = None
    bitrix_disk_folder_id: str | None = None
    bitrix_disk_folder_url: str | None = None
    linked_expertise_case_id: int | None = None
    snippets: list[str] = Field(default_factory=list)


class SiteDefectArchiveSearchResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[SiteDefectArchiveCaseListItem]


class SiteDefectArchiveCaseDetailResponse(SiteDefectArchiveCaseListItem):
    search_text: str
    messages: list[SiteDefectArchiveMessageResponse] = Field(default_factory=list)
    files: list[SiteDefectArchiveFileResponse] = Field(default_factory=list)


class SiteDefectWorkingAnalyzeRequest(BaseModel):
    title: str | None = None
    customer_contact: str | None = None
    order_refs: str | None = None
    product_model: str | None = None
    problem_description: str | None = None
    customer_request: str | None = None
    comments_text: str | None = None
    similar_limit: int = Field(default=5, ge=0, le=20)


class SiteDefectWorkingTaskRecommendation(BaseModel):
    role: str
    title: str
    due_hours: int
    reason: str


class SiteDefectWorkingSimilarArchiveCase(BaseModel):
    id: int
    title: str | None = None
    summary: str | None = None
    numbers: list[str] = Field(default_factory=list)
    problem_type: str | None = None
    bitrix_detail_url: str | None = None
    bitrix_disk_folder_url: str | None = None


class SiteDefectWorkingAnalyzeResponse(BaseModel):
    numbers: list[str] = Field(default_factory=list)
    problem_type: str
    problem_type_label: str
    priority: str
    recommended_stage: str
    recommended_stage_label: str
    recommended_tasks: list[SiteDefectWorkingTaskRecommendation] = Field(default_factory=list)
    similar_archive_cases: list[SiteDefectWorkingSimilarArchiveCase] = Field(default_factory=list)
    analysis_key: str
    comment: str


class SiteDefectWorkingReportResponse(BaseModel):
    status: str
    items_checked: int = 0
    buckets: dict[str, list[dict]] = Field(default_factory=dict)
    reason: str | None = None
