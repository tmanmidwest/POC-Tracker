"""Customer and contact CRUD endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.v1._helpers import raise_conflict_if_referenced
from app.db import get_db
from app.models import Contact, ContactRole, Customer, Project
from app.schemas.poc import (
    ContactCreate,
    ContactOut,
    ContactUpdate,
    CustomerCreate,
    CustomerDetailOut,
    CustomerOut,
    CustomerUpdate,
)
from app.services.audit import principal_actor, record_event
from app.services.auth import Principal, get_authenticated_principal

log = logging.getLogger(__name__)

router = APIRouter(prefix="/customers", tags=["customers"])


@router.get("/", response_model=list[CustomerOut])
def list_customers(
    db: Session = Depends(get_db),
    _principal: Principal = Depends(get_authenticated_principal),
) -> list[Customer]:
    return db.query(Customer).order_by(Customer.name).all()


@router.get("/{customer_id}", response_model=CustomerDetailOut)
def get_customer(
    customer_id: int,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(get_authenticated_principal),
) -> Customer:
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found.")
    return customer


@router.post("/", response_model=CustomerOut, status_code=status.HTTP_201_CREATED)
def create_customer(
    body: CustomerCreate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_authenticated_principal),
) -> Customer:
    customer = Customer(**body.model_dump())
    db.add(customer)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A customer named '{body.name}' already exists.",
        ) from None
    db.refresh(customer)
    record_event(
        category="customer",
        event_type="customer.created",
        **principal_actor(principal),
        target_type="customer",
        target_id=customer.id,
        target_label=customer.name,
        message=f"Created customer '{customer.name}'",
        detail={"surface": "api"},
    )
    return customer


@router.patch("/{customer_id}", response_model=CustomerOut)
def update_customer(
    customer_id: int,
    body: CustomerUpdate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_authenticated_principal),
) -> Customer:
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found.")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(customer, field, value)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A customer with that name already exists.",
        ) from None
    db.refresh(customer)
    record_event(
        category="customer",
        event_type="customer.updated",
        **principal_actor(principal),
        target_type="customer",
        target_id=customer.id,
        target_label=customer.name,
        message=f"Updated customer '{customer.name}'",
        detail={"surface": "api"},
    )
    return customer


@router.delete("/{customer_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_customer(
    customer_id: int,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_authenticated_principal),
) -> None:
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found.")
    raise_conflict_if_referenced(
        db=db,
        target_label=f"customer '{customer.name}'",
        references=[("projects", Project, Project.customer_id, customer_id)],
    )
    name = customer.name
    db.delete(customer)  # cascades to contacts
    db.commit()
    record_event(
        category="customer",
        event_type="customer.deleted",
        **principal_actor(principal),
        target_type="customer",
        target_id=customer_id,
        target_label=name,
        message=f"Deleted customer '{name}'",
        detail={"surface": "api"},
    )


# ---------------------------------------------------------------------------
# Contacts (nested under a customer)
# ---------------------------------------------------------------------------


@router.get("/{customer_id}/contacts", response_model=list[ContactOut])
def list_contacts(
    customer_id: int,
    db: Session = Depends(get_db),
    _principal: Principal = Depends(get_authenticated_principal),
) -> list[Contact]:
    if db.get(Customer, customer_id) is None:
        raise HTTPException(status_code=404, detail="Customer not found.")
    return (
        db.query(Contact)
        .filter(Contact.customer_id == customer_id)
        .order_by(Contact.name)
        .all()
    )


@router.post(
    "/{customer_id}/contacts",
    response_model=ContactOut,
    status_code=status.HTTP_201_CREATED,
)
def create_contact(
    customer_id: int,
    body: ContactCreate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_authenticated_principal),
) -> Contact:
    if db.get(Customer, customer_id) is None:
        raise HTTPException(status_code=404, detail="Customer not found.")
    if body.role_id is not None and db.get(ContactRole, body.role_id) is None:
        raise HTTPException(status_code=422, detail="Unknown contact role.")
    contact = Contact(customer_id=customer_id, **body.model_dump())
    db.add(contact)
    db.commit()
    db.refresh(contact)
    record_event(
        category="customer",
        event_type="contact.created",
        **principal_actor(principal),
        target_type="contact",
        target_id=contact.id,
        target_label=contact.name,
        message=f"Added contact '{contact.name}'",
        detail={"surface": "api", "customer_id": customer_id},
    )
    return contact


@router.patch("/contacts/{contact_id}", response_model=ContactOut)
def update_contact(
    contact_id: int,
    body: ContactUpdate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_authenticated_principal),
) -> Contact:
    contact = db.get(Contact, contact_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found.")
    if body.role_id is not None and db.get(ContactRole, body.role_id) is None:
        raise HTTPException(status_code=422, detail="Unknown contact role.")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(contact, field, value)
    db.commit()
    db.refresh(contact)
    record_event(
        category="customer",
        event_type="contact.updated",
        **principal_actor(principal),
        target_type="contact",
        target_id=contact.id,
        target_label=contact.name,
        message=f"Updated contact '{contact.name}'",
        detail={"surface": "api"},
    )
    return contact


@router.delete("/contacts/{contact_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_contact(
    contact_id: int,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_authenticated_principal),
) -> None:
    contact = db.get(Contact, contact_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found.")
    name = contact.name
    db.delete(contact)
    db.commit()
    record_event(
        category="customer",
        event_type="contact.deleted",
        **principal_actor(principal),
        target_type="contact",
        target_id=contact_id,
        target_label=name,
        message=f"Deleted contact '{name}'",
        detail={"surface": "api"},
    )
