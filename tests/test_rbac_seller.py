from fastapi import Depends, FastAPI, HTTPException
import httpx
import pytest
from sqlalchemy.orm import Session

from app import model, oauth2
from app.main import app


# ============================================================================
# 1. Seller Onboarding Endpoint Tests (POST /account/become-seller)
# ============================================================================

async def test_become_seller_success_upgrades_customer_role(
    client: httpx.AsyncClient,
    test_user: model.User,
    user_headers: dict[str, str],
    db_session: Session
):
    """
    Test customer onboarding as a seller:
    - Returns 201 Created with SellerProfile data.
    - Creates SellerProfile associated with the user in database.
    - Upgrades customer's role from 'customer' to 'seller'.
    """
    assert test_user.role == model.UserRole.CUSTOMER

    payload = {
        "business_name": "Galaxy Entertainment",
        "business_desc": "Concert and live event management"
    }

    response = await client.post(
        "/account/become-seller",
        json=payload,
        headers=user_headers
    )
    assert response.status_code == 201
    data = response.json()
    assert data["business_name"] == "Galaxy Entertainment"
    assert data["user_id"] == test_user.id

    # Verify database state
    db_session.refresh(test_user)
    assert test_user.role == model.UserRole.SELLER
    assert test_user.seller_profile is not None
    assert test_user.seller_profile.business_name == "Galaxy Entertainment"


async def test_become_seller_duplicate_profile_rejected(
    client: httpx.AsyncClient,
    seller_user: model.User,
    seller_headers: dict[str, str]
):
    """
    Test duplicate seller profile rejection:
    - User who already has a seller profile cannot register again.
    - Returns 400 Bad Request ("You are already registered as a seller.").
    """
    payload = {
        "business_name": "Second Business",
        "business_desc": "Duplicate attempt"
    }

    response = await client.post(
        "/account/become-seller",
        json=payload,
        headers=seller_headers
    )
    assert response.status_code == 400
    assert "already registered as a seller" in response.json()["detail"]


async def test_become_seller_preserves_admin_role(
    client: httpx.AsyncClient,
    admin_user: model.User,
    admin_headers: dict[str, str],
    db_session: Session
):
    """
    Test admin creating a seller profile:
    - Seller profile is created.
    - Role is preserved as ADMIN (not downgraded to SELLER).
    """
    assert admin_user.role == model.UserRole.ADMIN

    payload = {
        "business_name": "Official Admin Ticketing Org",
        "business_desc": "First-party system events"
    }

    response = await client.post(
        "/account/become-seller",
        json=payload,
        headers=admin_headers
    )
    assert response.status_code == 201

    db_session.refresh(admin_user)
    assert admin_user.role == model.UserRole.ADMIN
    assert admin_user.seller_profile is not None


async def test_become_seller_unauthenticated_or_unverified(
    client: httpx.AsyncClient,
    unverified_headers: dict[str, str]
):
    """
    Test unauthorized and unverified access to become-seller:
    - Unauthenticated returns 401.
    - Unverified user returns 403.
    """
    # 1. Unauthenticated
    resp_unauth = await client.post("/account/become-seller", json={"business_name": "Test"})
    assert resp_unauth.status_code == 401

    # 2. Unverified
    resp_unverified = await client.post(
        "/account/become-seller",
        json={"business_name": "Test"},
        headers=unverified_headers
    )
    assert resp_unverified.status_code == 403
    assert "Account is not verified" in resp_unverified.json()["detail"]


# ============================================================================
# 2. Role Guard Verification (RequireRole, require_seller, require_admin)
# ============================================================================

def test_role_guards_direct_call(
    test_user: model.User,
    seller_user: model.User,
    admin_user: model.User
):
    """
    Test RequireRole dependency guard logic directly:
    - require_seller allows seller & admin, rejects customer with 403.
    - require_admin allows admin, rejects seller & customer with 403.
    """
    # Customer evaluation
    with pytest.raises(HTTPException) as exc_seller_denied:
        oauth2.require_seller(test_user)
    assert exc_seller_denied.value.status_code == 403
    assert "requires one of the following roles: seller, admin" in exc_seller_denied.value.detail

    with pytest.raises(HTTPException) as exc_admin_denied:
        oauth2.require_admin(test_user)
    assert exc_admin_denied.value.status_code == 403
    assert "requires one of the following roles: admin" in exc_admin_denied.value.detail

    # Seller evaluation
    assert oauth2.require_seller(seller_user) == seller_user
    with pytest.raises(HTTPException) as exc_seller_admin_denied:
        oauth2.require_admin(seller_user)
    assert exc_seller_admin_denied.value.status_code == 403

    # Admin evaluation
    assert oauth2.require_seller(admin_user) == admin_user
    assert oauth2.require_admin(admin_user) == admin_user


async def test_role_guard_via_endpoint(
    client: httpx.AsyncClient,
    user_headers: dict[str, str],
    seller_headers: dict[str, str],
    admin_headers: dict[str, str]
):
    """
    Test role-guarded endpoints wired through FastAPI:
    - Customer denied access (403 Forbidden).
    - Seller allowed access to seller-only route, denied admin-only route.
    - Admin allowed access to both.
    """
    # Create temporary test endpoints to test guard integration
    @app.get("/test-seller-guard")
    def seller_only_route(user: model.User = Depends(oauth2.require_seller)):
        return {"access": "granted", "role": user.role}

    @app.get("/test-admin-guard")
    def admin_only_route(user: model.User = Depends(oauth2.require_admin)):
        return {"access": "granted", "role": user.role}

    # 1. Customer attempts
    cust_seller_resp = await client.get("/test-seller-guard", headers=user_headers)
    assert cust_seller_resp.status_code == 403
    cust_admin_resp = await client.get("/test-admin-guard", headers=user_headers)
    assert cust_admin_resp.status_code == 403

    # 2. Seller attempts
    seller_seller_resp = await client.get("/test-seller-guard", headers=seller_headers)
    assert seller_seller_resp.status_code == 200
    assert seller_seller_resp.json()["access"] == "granted"

    seller_admin_resp = await client.get("/test-admin-guard", headers=seller_headers)
    assert seller_admin_resp.status_code == 403

    # 3. Admin attempts
    admin_seller_resp = await client.get("/test-seller-guard", headers=admin_headers)
    assert admin_seller_resp.status_code == 200

    admin_admin_resp = await client.get("/test-admin-guard", headers=admin_headers)
    assert admin_admin_resp.status_code == 200
    assert admin_admin_resp.json()["access"] == "granted"
