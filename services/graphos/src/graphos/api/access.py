"""Users and grants: ``/v1/users`` (admin) and ``/v1/grants``.

Admins manage users and every grant.  A tenant's owner manages the grants on
that tenant (any level, ``owner`` included), by exact name only: patterns
and global rights (``create_tenants``, ``admin``) are an admin's.  graphos
refuses to remove its last admin.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response, status

from graphos import identity
from graphos.api.auth import deps
from graphos.auth import AdminPrincipal, OwnerPrincipal, ReadPrincipal
from graphos.contracts.auth import (
    GrantInfo,
    GrantLevel,
    GrantList,
    GrantSpec,
    UserCreate,
    UserCreated,
    UserInfo,
    UserList,
    UserUpdate,
)
from graphos.identity import Level, Principal
from graphos.oidc import GRANT_SUBJECT_PREFIXES

log = logging.getLogger(__name__)

router = APIRouter(tags=["access"])


def _user_info(user: identity.User) -> UserInfo:
    return UserInfo(
        name=user.name, kind=user.kind, disabled=user.disabled, must_change=user.must_change,  # type: ignore[arg-type]
        created_at=user.created_at, created_by=user.created_by, last_login_at=user.last_login_at,
    )


def _grant_info(grant: identity.Grant) -> GrantInfo:
    return GrantInfo(
        subject=grant.subject, tenant=grant.tenant_pattern, level=grant.level,  # type: ignore[arg-type]
        granted_by=grant.granted_by, granted_at=grant.granted_at,
    )


def _not_found(name: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no user '{name}'")


def _admins(store: identity.AccessStore) -> set[str]:
    """Enabled users holding ``admin``."""
    out = set()
    for grant in store.grants():
        if grant.level == "admin" and grant.subject.startswith("user:"):
            user = store.user(grant.subject.removeprefix("user:"))
            if user is not None and not user.disabled:
                out.add(user.name)
    return out


def _keep_an_admin(store: identity.AccessStore, losing: str) -> None:
    if _admins(store) == {losing}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"'{losing}' is the only admin; make another admin first",
        )


# ── Users (admin) ────────────────────────────────────────────────────────


@router.get("/users", response_model=UserList, summary="Users and service accounts")
async def list_users(request: Request, principal: AdminPrincipal) -> UserList:  # noqa: ARG001 — auth
    return UserList(users=[_user_info(u) for u in deps(request).store.users()])


@router.post(
    "/users",
    response_model=UserCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Create a person or a service account",
    description=(
        "The answer carries the password once. A person must change it at the first sign-in; "
        "a service account keeps it as its secret. Grant access with `POST /v1/grants`."
    ),
)
async def create_user(body: UserCreate, request: Request, principal: AdminPrincipal) -> UserCreated:
    store = deps(request).store
    if not identity.valid_user_name(body.name):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "a user name is lower case letters, digits, '_', '.', '-', "
                "starting with a letter"
            ),
        )
    try:
        user, password = await store.create_user(body.name, body.kind, by=principal.consumer)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    log.info("user created", extra={"user": user.name, "kind": user.kind, "by": principal.consumer})
    return UserCreated(user=_user_info(user), password=password)


@router.patch("/users/{name}", response_model=UserInfo, summary="Disable or enable a user")
async def update_user(
    name: str, body: UserUpdate, request: Request, principal: AdminPrincipal,
) -> UserInfo:
    store = deps(request).store
    if store.user(name) is None:
        raise _not_found(name)
    if body.disabled:
        if name == principal.consumer:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="you cannot disable yourself",
            )
        _keep_an_admin(store, name)
    await store.set_disabled(name, body.disabled)
    log.info(
        "user updated",
        extra={"user": name, "disabled": body.disabled, "by": principal.consumer},
    )
    user = store.user(name)
    assert user is not None
    return _user_info(user)


@router.delete(
    "/users/{name}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a user and its grants",
)
async def delete_user(name: str, request: Request, principal: AdminPrincipal) -> None:
    store = deps(request).store
    if store.user(name) is None:
        raise _not_found(name)
    if name == principal.consumer:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="you cannot delete yourself",
        )
    _keep_an_admin(store, name)
    await store.delete_user(name)
    log.info("user deleted", extra={"user": name, "by": principal.consumer})


@router.post(
    "/users/{name}:reset-secret",
    response_model=UserCreated,
    summary="Give a user a new password or secret",
    description=(
        "Shown once. Every token and session of the user stops working. A person must change "
        "it at the next sign-in; a service account uses it as its new secret."
    ),
)
async def reset_secret(name: str, request: Request, principal: AdminPrincipal) -> UserCreated:
    store = deps(request).store
    user = store.user(name)
    if user is None:
        raise _not_found(name)
    secret = identity.new_secret()
    await store.set_password(name, secret, must_change=user.kind == "person")
    log.info("user secret reset", extra={"user": name, "by": principal.consumer})
    updated = store.user(name)
    assert updated is not None
    return UserCreated(user=_user_info(updated), password=secret)


# ── Grants ───────────────────────────────────────────────────────────────


def _may_manage(principal: Principal, spec: GrantSpec) -> None:
    """403 unless ``principal`` may give or take ``spec``."""
    if principal.admin:
        return
    if spec.level in identity.GLOBAL_RIGHTS or "*" in spec.tenant:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="patterns, 'create_tenants' and 'admin' are granted by an admin",
        )
    if not principal.has(spec.tenant, Level.OWNER):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"managing access to tenant '{spec.tenant}' needs 'owner' on it",
        )


def _validate(store: identity.AccessStore, spec: GrantSpec) -> None:
    def invalid(detail: str) -> HTTPException:
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)

    if spec.subject.startswith("user:"):
        if store.user(spec.subject.removeprefix("user:")) is None:
            raise invalid(f"subject '{spec.subject}' is not an existing user")
    elif not spec.subject.startswith(GRANT_SUBJECT_PREFIXES) or spec.subject[-1] == ":":
        raise invalid(
            f"subject '{spec.subject}' is not user:<name>, oidc-sub:<sub>, "
            "oidc-group:<group>, oidc-role:<role> or oidc-client:<client id>",
        )
    if not identity.valid_pattern(spec.tenant):
        raise invalid(f"'{spec.tenant}' is not a tenant name or pattern")
    if spec.level == "admin" and spec.tenant != "*":
        raise invalid("'admin' is granted on '*'")


@router.get(
    "/grants",
    response_model=GrantList,
    summary="Grants",
    description=(
        "Admins see every grant. Others see their own, and every grant on the tenants they own."
    ),
)
async def list_grants(
    request: Request,
    principal: ReadPrincipal,
    subject: Annotated[str | None, Query(description="Only this subject's.")] = None,
    tenant: Annotated[
        str | None, Query(description="Only those that apply to this tenant."),
    ] = None,
) -> GrantList:
    found = deps(request).store.grants(subject, tenant)
    if not principal.admin:
        found = [
            g for g in found
            if g.subject == principal.subject
            or ("*" not in g.tenant_pattern and g.level not in identity.GLOBAL_RIGHTS
                and principal.has(g.tenant_pattern, Level.OWNER))
        ]
    return GrantList(grants=[_grant_info(g) for g in found])


@router.post(
    "/grants",
    response_model=GrantInfo,
    status_code=status.HTTP_201_CREATED,
    summary="Grant a level on a tenant (or pattern), or a global right",
    description=(
        "Admins grant anything. A tenant's owner grants levels on that tenant (exact name). "
        "Answers 200 when the grant already existed."
    ),
)
async def create_grant(
    spec: GrantSpec, request: Request, response: Response, principal: OwnerPrincipal,
) -> GrantInfo:
    store = deps(request).store
    _may_manage(principal, spec)
    _validate(store, spec)
    if not await store.grant(spec.subject, spec.tenant, spec.level, by=principal.consumer):
        response.status_code = status.HTTP_200_OK
    log.info(
        "access granted",
        extra={
            "subject": spec.subject, "tenant": spec.tenant, "level": spec.level,
            "by": principal.consumer,
        },
    )
    grant = next(
        g for g in store.grants(spec.subject)
        if g.tenant_pattern == spec.tenant and g.level == spec.level
    )
    return _grant_info(grant)


@router.delete(
    "/grants",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a grant",
    description="The same rights as granting. Applies at the subject's next request.",
)
async def revoke_grant(
    request: Request,
    principal: OwnerPrincipal,
    subject: Annotated[str, Query()],
    tenant: Annotated[str, Query()],
    level: Annotated[GrantLevel, Query()],
) -> None:
    store = deps(request).store
    spec = GrantSpec(subject=subject, tenant=tenant, level=level)
    _may_manage(principal, spec)
    if level == "admin":
        _keep_an_admin(store, subject.removeprefix("user:"))
    if not await store.revoke(subject, tenant, level):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such grant")
    log.info(
        "access revoked",
        extra={"subject": subject, "tenant": tenant, "level": level, "by": principal.consumer},
    )
