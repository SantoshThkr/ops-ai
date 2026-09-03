from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

from app.models import (
    ActionStatus,
    ApprovalDecision,
    DocumentStatus,
    IncidentStatus,
    UserRole,
)


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Name must not be blank")
        return value


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: EmailStr
    name: str
    role: UserRole
    active: bool


class AuthResponse(BaseModel):
    user: UserResponse


class DocumentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    filename: str
    content_type: str
    file_size: int
    checksum: str
    status: DocumentStatus
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class DocumentListResponse(BaseModel):
    items: list[DocumentResponse]
    page: int
    page_size: int
    total: int


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int | None = Field(default=None, ge=1, le=50)


class SearchResult(BaseModel):
    document_id: UUID
    chunk_id: UUID | None = None
    filename: str
    content: str
    page_number: int | None
    similarity: float


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=20000)


class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    conversation_id: UUID
    role: str
    content: str
    created_at: datetime


class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)


class ConversationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    title: str
    created_at: datetime
    updated_at: datetime


class ConversationDetailResponse(ConversationResponse):
    messages: list[MessageResponse] = []


class ConversationListResponse(BaseModel):
    items: list[ConversationResponse]
    page: int
    page_size: int
    total: int


class CitationResponse(BaseModel):
    document_id: UUID
    filename: str
    chunk_id: UUID | None = None
    page_number: int | None = None


class MetricResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    service: str
    name: str
    value: float
    unit: str
    observed_at: datetime
    metadata: dict[str, object] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("metric_metadata", "metadata"),
    )


class MetricsQuery(BaseModel):
    service: str | None = Field(default=None, min_length=1, max_length=120)
    name: str | None = Field(default=None, min_length=1, max_length=120)


class ProposedAction(BaseModel):
    kind: Literal["restart_service", "rollback_deployment"]
    parameters: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_parameters(self) -> "ProposedAction":
        allowed = {"service"} if self.kind == "restart_service" else {"deployment", "version"}
        if set(self.parameters) - allowed:
            raise ValueError("Action parameters are not allowed for this action")
        service = self.parameters.get("service")
        if self.kind == "restart_service" and (
            not isinstance(service, str)
            or service
            not in {
                "api",
                "worker",
                "web",
            }
        ):
            raise ValueError("A restart action requires an allowed service")
        if self.kind == "rollback_deployment" and not isinstance(
            self.parameters.get("deployment"), str
        ):
            raise ValueError("A rollback action requires a deployment")
        return self


class IncidentProposalCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=5000)
    severity: str = Field(default="medium", pattern="^(low|medium|high|critical)$")
    action_kind: Literal["restart_service", "rollback_deployment"] | None = None
    action_parameters: dict[str, object] = Field(default_factory=dict)
    expires_in_minutes: int = Field(default=30, ge=1, le=1440)
    conversation_id: UUID | None = None
    idempotency_key: str | None = Field(default=None, max_length=200)
    actions: list[ProposedAction] = Field(default_factory=list, max_length=10)

    @field_validator("title", "summary")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Text must not be blank")
        return value

    @model_validator(mode="after")
    def validate_action(self) -> "IncidentProposalCreate":
        if self.action_kind is not None:
            ProposedAction(kind=self.action_kind, parameters=self.action_parameters)
        return self


class ActionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    incident_id: UUID
    owner_id: UUID
    kind: str
    parameters: dict[str, object]
    status: ActionStatus
    expires_at: datetime
    approved_by: UUID | None
    executed_at: datetime | None
    result: dict[str, object] | None


class IncidentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    owner_id: UUID
    conversation_id: UUID | None
    title: str
    summary: str
    severity: str
    status: IncidentStatus
    created_at: datetime
    updated_at: datetime
    actions: list[ActionResponse] = Field(default_factory=list)


class ApprovalCreate(BaseModel):
    decision: ApprovalDecision | None = None
    approved: bool | None = None

    @model_validator(mode="after")
    def normalize_decision(self) -> "ApprovalCreate":
        if self.decision is None and self.approved is None:
            raise ValueError("decision or approved is required")
        if self.decision is not None and self.approved is not None:
            expected = ApprovalDecision.APPROVED if self.approved else ApprovalDecision.REJECTED
            if self.decision != expected:
                raise ValueError("decision and approved disagree")
        if self.decision is None:
            self.decision = (
                ApprovalDecision.APPROVED if self.approved else ApprovalDecision.REJECTED
            )
        return self


class ApprovalResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    action_id: UUID
    approver_id: UUID
    decision: ApprovalDecision
    idempotency_key: str | None
    created_at: datetime


class AuditLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    actor_id: UUID | None
    event: str
    resource_type: str
    resource_id: str | None
    details: dict[str, object]
    created_at: datetime
