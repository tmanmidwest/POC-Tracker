"""HTML UI for managing customers and their contacts."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AppUser, Contact, ContactRole, Customer, Project
from app.services.audit import record_event
from app.ui.dependencies import require_ui_user
from app.ui.flash import flash
from app.ui.templating import render

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ui/customers", tags=["ui"], include_in_schema=False)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    v = value.strip()
    return v or None


@router.get("/")
def list_customers(
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    customers = db.query(Customer).order_by(Customer.name).all()
    return render(
        request,
        "customers/list.html",
        current_user=user,
        active_section="customers",
        customers=customers,
    )


@router.get("/new")
def new_form(
    request: Request,
    user: AppUser = Depends(require_ui_user),
) -> Response:
    return render(
        request,
        "customers/form.html",
        current_user=user,
        active_section="customers",
        customer=None,
        form={},
        form_action="/ui/customers/new",
    )


@router.post("/new")
def create_customer(
    request: Request,
    name: str = Form(...),
    website: str | None = Form(None),
    notes: str | None = Form(None),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    customer = Customer(name=_clean(name), website=_clean(website), notes=_clean(notes))
    if not customer.name:
        return render(
            request, "customers/form.html", current_user=user,
            active_section="customers", customer=None,
            form={"name": name, "website": website, "notes": notes},
            form_action="/ui/customers/new", error="Customer name is required.",
        )
    db.add(customer)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return render(
            request, "customers/form.html", current_user=user,
            active_section="customers", customer=None,
            form={"name": name, "website": website, "notes": notes},
            form_action="/ui/customers/new",
            error=f"A customer named '{name}' already exists.",
        )
    record_event(
        category="customer", event_type="customer.created", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="customer",
        target_id=customer.id, target_label=customer.name,
        message=f"Created customer '{customer.name}'",
        detail={"surface": "ui"}, request=request,
    )
    flash(request, f"Customer '{customer.name}' created.", "success")
    return RedirectResponse(url=f"/ui/customers/{customer.id}", status_code=303)


@router.get("/{customer_id}")
def detail(
    customer_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found.")
    roles = (
        db.query(ContactRole)
        .filter(ContactRole.is_active.is_(True))
        .order_by(ContactRole.name)
        .all()
    )
    projects = (
        db.query(Project)
        .filter(Project.customer_id == customer_id)
        .order_by(Project.id.desc())
        .all()
    )
    return render(
        request, "customers/detail.html", current_user=user,
        active_section="customers", customer=customer, roles=roles, projects=projects,
    )


@router.get("/{customer_id}/edit")
def edit_form(
    customer_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found.")
    return render(
        request, "customers/form.html", current_user=user,
        active_section="customers", customer=customer,
        form={"name": customer.name, "website": customer.website, "notes": customer.notes},
        form_action=f"/ui/customers/{customer_id}/edit",
    )


@router.post("/{customer_id}/edit")
def update_customer(
    customer_id: int,
    request: Request,
    name: str = Form(...),
    website: str | None = Form(None),
    notes: str | None = Form(None),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found.")
    customer.name = _clean(name) or customer.name
    customer.website = _clean(website)
    customer.notes = _clean(notes)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        flash(request, "A customer with that name already exists.", "error")
        return RedirectResponse(url=f"/ui/customers/{customer_id}/edit", status_code=303)
    record_event(
        category="customer", event_type="customer.updated", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="customer",
        target_id=customer.id, target_label=customer.name,
        message=f"Updated customer '{customer.name}'",
        detail={"surface": "ui"}, request=request,
    )
    flash(request, "Customer updated.", "success")
    return RedirectResponse(url=f"/ui/customers/{customer_id}", status_code=303)


@router.post("/{customer_id}/delete")
def delete_customer(
    customer_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found.")
    project_count = (
        db.query(Project).filter(Project.customer_id == customer_id).count()
    )
    if project_count:
        flash(
            request,
            f"Cannot delete '{customer.name}': it still has {project_count} project(s).",
            "error",
        )
        return RedirectResponse(url=f"/ui/customers/{customer_id}", status_code=303)
    name = customer.name
    db.delete(customer)
    db.commit()
    record_event(
        category="customer", event_type="customer.deleted", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="customer",
        target_id=customer_id, target_label=name,
        message=f"Deleted customer '{name}'", detail={"surface": "ui"}, request=request,
    )
    flash(request, f"Customer '{name}' deleted.", "success")
    return RedirectResponse(url="/ui/customers", status_code=303)


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------


@router.post("/{customer_id}/contacts")
def add_contact(
    customer_id: int,
    request: Request,
    name: str = Form(...),
    email: str | None = Form(None),
    phone: str | None = Form(None),
    role_id: str | None = Form(None),
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found.")
    if not _clean(name):
        flash(request, "Contact name is required.", "error")
        return RedirectResponse(url=f"/ui/customers/{customer_id}", status_code=303)
    contact = Contact(
        customer_id=customer_id,
        name=_clean(name),
        email=_clean(email),
        phone=_clean(phone),
        role_id=int(role_id) if role_id else None,
    )
    db.add(contact)
    db.commit()
    record_event(
        category="customer", event_type="contact.created", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="contact",
        target_id=contact.id, target_label=contact.name,
        message=f"Added contact '{contact.name}' to '{customer.name}'",
        detail={"surface": "ui"}, request=request,
    )
    flash(request, f"Contact '{contact.name}' added.", "success")
    return RedirectResponse(url=f"/ui/customers/{customer_id}", status_code=303)


@router.post("/contacts/{contact_id}/delete")
def delete_contact(
    contact_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: AppUser = Depends(require_ui_user),
) -> Response:
    contact = db.get(Contact, contact_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found.")
    customer_id = contact.customer_id
    name = contact.name
    db.delete(contact)
    db.commit()
    record_event(
        category="customer", event_type="contact.deleted", actor_type="user",
        actor_label=user.username, actor_id=user.id, target_type="contact",
        target_id=contact_id, target_label=name,
        message=f"Deleted contact '{name}'", detail={"surface": "ui"}, request=request,
    )
    flash(request, f"Contact '{name}' removed.", "success")
    return RedirectResponse(url=f"/ui/customers/{customer_id}", status_code=303)
