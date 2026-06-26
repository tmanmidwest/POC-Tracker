"""SQLAlchemy ORM models for POC Tracker.

Import order matters here because of foreign keys — leaf/lookup tables first,
then tables that reference them.
"""

from app.models.api_key import ApiKey
from app.models.app_branding import AppBranding
from app.models.app_config import AppConfig
from app.models.app_user import AppUser
from app.models.audit_event import AuditEvent
from app.models.auth_provider import AuthProvider
from app.models.contact import Contact
from app.models.contact_role import ContactRole
from app.models.customer import Customer
from app.models.dashboard_pref import DashboardPref
from app.models.feature_type import FeatureType
from app.models.note_attachment import NoteAttachment
from app.models.oauth_client import OAuthClient
from app.models.project import Project
from app.models.project_note import ProjectNote
from app.models.project_status import ProjectStatus
from app.models.project_use_case import ProjectUseCase
from app.models.screenshot import Screenshot
from app.models.use_case_library import UseCaseLibrary
from app.models.use_case_status import UseCaseStatus
from app.models.user_identity import UserIdentity

__all__ = [
    "ApiKey",
    "AppBranding",
    "AppConfig",
    "AppUser",
    "AuditEvent",
    "AuthProvider",
    "Contact",
    "ContactRole",
    "Customer",
    "DashboardPref",
    "FeatureType",
    "NoteAttachment",
    "OAuthClient",
    "Project",
    "ProjectNote",
    "ProjectStatus",
    "ProjectUseCase",
    "Screenshot",
    "UseCaseLibrary",
    "UseCaseStatus",
    "UserIdentity",
]
