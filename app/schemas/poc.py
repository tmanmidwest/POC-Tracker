"""Pydantic schemas for the core POC domain: customers, contacts, projects,
the use-case library, and project use cases.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Lightweight nested references
# ---------------------------------------------------------------------------


class NamedRef(BaseModel):
    """Generic {id, name} reference embedded in responses."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str


class UserRef(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str


# ---------------------------------------------------------------------------
# Contact
# ---------------------------------------------------------------------------


class ContactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    customer_id: int
    name: str
    email: str | None
    phone: str | None
    role_id: int | None
    role: NamedRef | None
    created_at: datetime
    updated_at: datetime


class ContactCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=50)
    role_id: int | None = None


class ContactUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=50)
    role_id: int | None = None


# ---------------------------------------------------------------------------
# Customer
# ---------------------------------------------------------------------------


class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    website: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime


class CustomerDetailOut(CustomerOut):
    contacts: list[ContactOut] = []


class CustomerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    website: str | None = Field(default=None, max_length=255)
    notes: str | None = None


class CustomerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    website: str | None = Field(default=None, max_length=255)
    notes: str | None = None


# ---------------------------------------------------------------------------
# Use Case Library
# ---------------------------------------------------------------------------


class UseCaseLibraryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    category: str
    default_reference_number: str | None
    name: str
    description: str | None
    success_validation: str | None
    feature_type_id: int | None
    feature_type: NamedRef | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class UseCaseLibraryCreate(BaseModel):
    category: str = Field(min_length=1, max_length=150)
    default_reference_number: str | None = Field(default=None, max_length=20)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    success_validation: str | None = None
    feature_type_id: int | None = None
    is_active: bool = True


class UseCaseLibraryUpdate(BaseModel):
    category: str | None = Field(default=None, min_length=1, max_length=150)
    default_reference_number: str | None = Field(default=None, max_length=20)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    success_validation: str | None = None
    feature_type_id: int | None = None
    is_active: bool | None = None


# ---------------------------------------------------------------------------
# Screenshot
# ---------------------------------------------------------------------------


class ScreenshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_use_case_id: int
    original_filename: str | None
    content_type: str | None
    size_bytes: int | None
    caption: str | None
    created_at: datetime


# ---------------------------------------------------------------------------
# Project Use Case
# ---------------------------------------------------------------------------


class ProjectUseCaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: int
    source: str
    library_id: int | None
    reference_number: str | None
    category: str
    name: str
    description: str | None
    success_validation: str | None
    feature_type_id: int | None
    feature_type: NamedRef | None
    status_id: int
    status: NamedRef
    comments: str | None
    completed_on: date | None
    screenshots: list[ScreenshotOut] = []
    created_at: datetime
    updated_at: datetime


class ProjectUseCaseCreate(BaseModel):
    """Add an ad-hoc (custom) use case to a project."""

    reference_number: str | None = Field(default=None, max_length=20)
    category: str = Field(min_length=1, max_length=150)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    success_validation: str | None = None
    feature_type_id: int | None = None
    status_id: int | None = None
    comments: str | None = None
    completed_on: date | None = None


class ProjectUseCaseUpdate(BaseModel):
    reference_number: str | None = Field(default=None, max_length=20)
    category: str | None = Field(default=None, min_length=1, max_length=150)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    success_validation: str | None = None
    feature_type_id: int | None = None
    status_id: int | None = None
    comments: str | None = None
    completed_on: date | None = None


class AddLibraryUseCases(BaseModel):
    """Copy one or more library entries into a project as snapshots."""

    library_ids: list[int] = Field(min_length=1)


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    customer_id: int
    customer: NamedRef
    name: str | None
    status_id: int
    status: NamedRef
    start_date: date | None
    end_date: date | None
    sales_engineer_id: int | None
    sales_engineer: UserRef | None
    account_executive: str | None
    account_executive_email: str | None
    salesforce_opp_url: str | None
    notebook_url: str | None
    poc_instance_url: str | None
    notes: str | None
    is_archived: bool
    created_at: datetime
    updated_at: datetime


class ProjectDetailOut(ProjectOut):
    use_cases: list[ProjectUseCaseOut] = []


class ProjectCreate(BaseModel):
    customer_id: int
    name: str | None = Field(default=None, max_length=200)
    status_id: int | None = None
    start_date: date | None = None
    end_date: date | None = None
    sales_engineer_id: int | None = None
    account_executive: str | None = Field(default=None, max_length=200)
    account_executive_email: str | None = Field(default=None, max_length=255)
    salesforce_opp_url: str | None = Field(default=None, max_length=1000)
    notebook_url: str | None = Field(default=None, max_length=1000)
    poc_instance_url: str | None = Field(default=None, max_length=1000)
    notes: str | None = None


class ProjectUpdate(BaseModel):
    customer_id: int | None = None
    name: str | None = Field(default=None, max_length=200)
    status_id: int | None = None
    start_date: date | None = None
    end_date: date | None = None
    sales_engineer_id: int | None = None
    account_executive: str | None = Field(default=None, max_length=200)
    account_executive_email: str | None = Field(default=None, max_length=255)
    salesforce_opp_url: str | None = Field(default=None, max_length=1000)
    notebook_url: str | None = Field(default=None, max_length=1000)
    poc_instance_url: str | None = Field(default=None, max_length=1000)
    notes: str | None = None
    is_archived: bool | None = None
