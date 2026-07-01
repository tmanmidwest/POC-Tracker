"""SQLAlchemy ORM models for POC Tracker.

Import order matters here because of foreign keys — leaf/lookup tables first,
then tables that reference them.
"""

from app.models.ai_provider import AIProvider
from app.models.api_key import ApiKey
from app.models.app_branding import AppBranding
from app.models.app_config import AppConfig
from app.models.app_user import AppUser
from app.models.audit_event import AuditEvent
from app.models.auth_provider import AuthProvider
from app.models.backup_run import BackupRun
from app.models.contact import Contact
from app.models.contact_role import ContactRole
from app.models.customer import Customer
from app.models.dashboard_pref import DashboardPref
from app.models.feature_type import FeatureType
from app.models.google_tasks_config import GoogleTasksConfig
from app.models.library_set import LibrarySet
from app.models.mcp_gateway_token import McpGatewayToken
from app.models.note_attachment import NoteAttachment
from app.models.oauth_client import OAuthClient
from app.models.project import Project
from app.models.project_grant import ProjectGrant
from app.models.project_note import ProjectNote
from app.models.project_status import ProjectStatus
from app.models.project_use_case import ProjectUseCase
from app.models.screenshot import Screenshot
from app.models.task import Task
from app.models.task_dashboard_pref import TaskDashboardPref
from app.models.task_priority import TaskPriority
from app.models.task_status import TaskStatus
from app.models.use_case_library import UseCaseLibrary
from app.models.use_case_status import UseCaseStatus
from app.models.use_case_view_pref import UseCaseViewPref
from app.models.user_google_credential import UserGoogleCredential
from app.models.user_identity import UserIdentity

__all__ = [
    "AIProvider",
    "ApiKey",
    "AppBranding",
    "AppConfig",
    "AppUser",
    "AuditEvent",
    "AuthProvider",
    "BackupRun",
    "Contact",
    "ContactRole",
    "Customer",
    "DashboardPref",
    "FeatureType",
    "GoogleTasksConfig",
    "LibrarySet",
    "McpGatewayToken",
    "NoteAttachment",
    "OAuthClient",
    "Project",
    "ProjectGrant",
    "ProjectNote",
    "ProjectStatus",
    "ProjectUseCase",
    "Screenshot",
    "Task",
    "TaskDashboardPref",
    "TaskPriority",
    "TaskStatus",
    "UseCaseLibrary",
    "UseCaseStatus",
    "UseCaseViewPref",
    "UserGoogleCredential",
    "UserIdentity",
]
