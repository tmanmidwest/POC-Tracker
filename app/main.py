"""FastAPI application entry point.

Run with:
    python -m app.main
or:
    uvicorn app.main:app --host 0.0.0.0 --port 8010
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app import __version__
from app.config import get_settings
from app.db import get_engine
from app.logging_config import configure_logging

log = logging.getLogger(__name__)

# How often the background task re-checks the audit retention window.
_AUDIT_PRUNE_INTERVAL_SECONDS = 24 * 60 * 60


async def _audit_retention_loop() -> None:
    """Prune expired audit events once a day while the app is running.

    Startup pruning alone isn't enough for a long-lived instance that runs for
    weeks without a restart, so this keeps the retention window enforced.
    """
    from app.services.audit import prune_old_events

    while True:
        await asyncio.sleep(_AUDIT_PRUNE_INTERVAL_SECONDS)
        # prune_old_events does its own DB work and never raises; run it in a
        # worker thread so the event loop isn't blocked.
        await asyncio.to_thread(prune_old_events)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> Any:
    """Application lifespan: startup and shutdown hooks."""
    settings = get_settings()
    settings.ensure_data_dir()

    # Apply any staged restore BEFORE the engine/migrations touch the DB, so the
    # restored files are swapped in first; migrations then bring the restored
    # schema up to head. Safe here because nothing has opened the DB yet.
    from app.services.backups import apply_pending_restore

    if apply_pending_restore():
        log.info("startup_restore_applied")

    # Run migrations FIRST (before any DB access), then seed
    from app.db import get_session_factory
    from app.services.audit import prune_old_events
    from app.services.migrations import run_migrations
    from app.services.seed_data import seed_database

    run_migrations()

    SessionLocal = get_session_factory()
    with SessionLocal() as db:
        seed_database(db, settings)

    # Enforce the audit retention window once at startup, then daily.
    from app.services import system_config

    prune_old_events()
    retention_task = asyncio.create_task(_audit_retention_loop())

    # Trigger engine creation early so we fail fast on bad config
    engine = get_engine()
    log.info(
        "app_startup",
        extra={
            "app_name": settings.app_name,
            "app_version": settings.app_version,
            "data_dir": str(settings.data_dir),
            "database_url": settings.database_url,
            "audit_retention_days": system_config.current_retention_days(),
        },
    )

    yield

    retention_task.cancel()
    try:
        await retention_task
    except asyncio.CancelledError:
        pass
    engine.dispose()
    log.info("app_shutdown")


def create_app() -> FastAPI:
    """Build and return the FastAPI application instance."""
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "POC Tracker — manage proof-of-concept engagements: customers, "
            "projects, use cases, and reporting, with a REST API and MCP access."
        ),
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # Session middleware (for UI login cookies)
    # Secret persists across container restarts so sessions survive a redeploy.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.get_or_create_session_secret(),
        max_age=settings.session_max_age_seconds,
        same_site="lax",
        https_only=False,  # set True behind an HTTPS-terminating proxy
        session_cookie="poct_session",
    )

    # --- Routers ---
    from app.api.v1.api_keys import router as api_keys_router
    from app.api.v1.customers import router as customers_router
    from app.api.v1.lookups import (
        contact_roles_router,
        feature_types_router,
        project_statuses_router,
        use_case_statuses_router,
    )
    from app.api.v1.oauth_clients import router as oauth_clients_router
    from app.api.v1.oauth_token import router as oauth_token_router
    from app.api.v1.projects import router as projects_router
    from app.api.v1.session_auth import router as session_auth_router
    from app.api.v1.use_case_library import router as use_case_library_router

    # /api/v1/auth/* (session login, API keys, OAuth client management)
    app.include_router(session_auth_router, prefix="/api/v1")
    app.include_router(api_keys_router, prefix="/api/v1")
    app.include_router(oauth_clients_router, prefix="/api/v1")

    # /api/v1/* (lookups)
    app.include_router(contact_roles_router, prefix="/api/v1")
    app.include_router(project_statuses_router, prefix="/api/v1")
    app.include_router(feature_types_router, prefix="/api/v1")
    app.include_router(use_case_statuses_router, prefix="/api/v1")

    # /api/v1/* (core POC domain)
    app.include_router(customers_router, prefix="/api/v1")
    app.include_router(use_case_library_router, prefix="/api/v1")
    app.include_router(projects_router, prefix="/api/v1")

    # /oauth/token (RFC 6749 - mounted at root, not under /api/v1)
    app.include_router(oauth_token_router)

    # --- UI routers ---
    from fastapi import Depends

    from app.ui.audit_routes import router as ui_audit_router
    from app.ui.auth_routes import router as ui_auth_router
    from app.ui.customer_routes import router as ui_customer_router
    from app.ui.dashboard_routes import router as ui_dashboard_router
    from app.ui.dependencies import (
        Forbidden,
        RedirectToLogin,
        forbidden_handler,
        redirect_to_login_handler,
        require_admin_ui,
        require_internal_ui,
    )
    from app.ui.grant_routes import router as ui_grant_router
    from app.ui.library_routes import router as ui_library_router
    from app.ui.lookup_routes import router as ui_lookup_router
    from app.ui.oidc_routes import router as ui_oidc_router
    from app.ui.project_routes import router as ui_project_router
    from app.ui.report_routes import router as ui_report_router
    from app.ui.search_routes import router as ui_search_router
    from app.ui.settings_routes import router as ui_settings_router

    # Open to any logged-in user (standard, admin, or external viewer). The
    # routes themselves scope what an external viewer can see.
    app.include_router(ui_auth_router)
    app.include_router(ui_oidc_router)
    app.include_router(ui_dashboard_router)
    app.include_router(ui_project_router)
    app.include_router(ui_report_router)
    app.include_router(ui_search_router)

    # Open to internal users only (standard or admin) — hidden from external viewers.
    internal_only = [Depends(require_internal_ui)]
    app.include_router(ui_customer_router, dependencies=internal_only)
    app.include_router(ui_audit_router, dependencies=internal_only)
    app.include_router(ui_grant_router)  # routes self-check can_grant_project

    # Admin-only surfaces — gated at the router level.
    admin_only = [Depends(require_admin_ui)]
    app.include_router(ui_library_router, dependencies=admin_only)
    app.include_router(ui_lookup_router, dependencies=admin_only)
    app.include_router(ui_settings_router, dependencies=admin_only)

    # Handler that turns the UI's "you need to log in" exception into a 303
    # redirect to the login page with ?next=<original-url>.
    app.add_exception_handler(RedirectToLogin, redirect_to_login_handler)
    # Logged-in but not an admin → bounce to the dashboard with a flash.
    app.add_exception_handler(Forbidden, forbidden_handler)

    # --- Static files ---
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # --- Meta endpoints ---

    @app.get("/health", tags=["meta"])
    async def health() -> JSONResponse:
        """Liveness probe. Returns 200 if the app is up and DB is reachable."""
        db_status = "ok"
        try:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:
            log.warning("health_db_check_failed", extra={"error": str(exc)})
            db_status = "error"

        status_code = 200 if db_status == "ok" else 503
        return JSONResponse(
            status_code=status_code,
            content={
                "status": "ok" if db_status == "ok" else "degraded",
                "database": db_status,
                "version": __version__,
            },
        )

    @app.get("/", tags=["meta"], include_in_schema=False)
    async def root() -> RedirectResponse:
        """Redirect the root URL to the UI."""
        return RedirectResponse(url="/ui/dashboard", status_code=307)

    return app


app = create_app()


def main() -> None:
    """Module entry point for `python -m app.main`."""
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.bind_host,
        port=settings.bind_port,
        log_config=None,  # we configure logging ourselves
        access_log=False,
        # Honor X-Forwarded-Proto/-For from the load balancer so the app knows
        # it's being served over HTTPS (correct scheme in redirects, OAuth
        # redirect URIs, etc.). The container is only reachable from the ALB,
        # so trusting forwarded headers from any peer is safe here.
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
