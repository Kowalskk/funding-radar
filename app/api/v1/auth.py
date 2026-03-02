"""
app/api/v1/auth.py — Authentication and user management endpoints.

Routes:
  POST /auth/register  — create account
  POST /auth/login     — exchange credentials for JWT
  GET  /auth/me        — current user info
  POST /auth/api-key   — regenerate API key
  POST /auth/checkout  — Stripe checkout session for Pro upgrade
"""


from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field

from app.dependencies import get_current_user, redis_rate_limit
from app.models.db.user import User
from app.services.auth_service import (
    create_access_token,
    create_refresh_token,
    generate_api_key,
    hash_password,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["Auth"])


# ── Request / Response schemas ────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=72)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    tier: str


class UserResponse(BaseModel):
    id: int
    email: str
    tier: str
    api_key: str
    has_telegram: bool
    has_stripe: bool


class CheckoutRequest(BaseModel):
    success_url: str = Field(..., description="Redirect URL after successful payment")
    cancel_url: str = Field(..., description="Redirect URL if user cancels")


# ── POST /auth/register ───────────────────────────────────────────────────────

@router.post("/register", status_code=status.HTTP_201_CREATED, response_model=TokenResponse)
async def register(
    body: RegisterRequest,
    _rl: None = Depends(redis_rate_limit),
) -> Any:
    """Create a new user account (free tier)."""
    from app.core.database import get_db_session
    from sqlalchemy import select

    async with get_db_session() as session:
        existing = await session.scalar(
            select(User.id).where(User.email == body.email)
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An account with this email already exists.",
            )

        try:
            hashed = hash_password(body.password)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc

        user = User(
            email=body.email,
            hashed_password=hashed,
            api_key=generate_api_key(),
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

    return TokenResponse(
        access_token=create_access_token(user.id, user.tier.value),
        refresh_token=create_refresh_token(user.id),
        tier=user.tier.value,
    )



# ── POST /auth/login ──────────────────────────────────────────────────────────

@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    _rl: None = Depends(redis_rate_limit),
) -> Any:
    """Exchange email + password for JWT tokens."""
    from app.core.database import get_db_session
    from sqlalchemy import select

    async with get_db_session() as session:
        user = await session.scalar(
            select(User).where(User.email == body.email, User.is_active.is_(True))
        )

    if user is None or not user.hashed_password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not verify_password(body.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return TokenResponse(
        access_token=create_access_token(user.id, user.tier.value),
        refresh_token=create_refresh_token(user.id),
        tier=user.tier.value,
    )


# ── GET /auth/me ──────────────────────────────────────────────────────────────

@router.get("/me", response_model=UserResponse)
async def get_me(
    current_user: User = Depends(get_current_user),
    _rl: None = Depends(redis_rate_limit),
) -> Any:
    """Return the currently authenticated user's profile."""
    return UserResponse(
        id=current_user.id,
        email=current_user.email,
        tier=current_user.tier.value,
        api_key=current_user.api_key,
        has_telegram=current_user.telegram_chat_id is not None,
        has_stripe=current_user.stripe_customer_id is not None,
    )


# ── POST /auth/api-key ────────────────────────────────────────────────────────

@router.post("/api-key", response_model=dict)
async def regenerate_api_key(
    current_user: User = Depends(get_current_user),
    _rl: None = Depends(redis_rate_limit),
) -> Any:
    """Generate a new API key, invalidating the old one."""
    from app.core.database import get_db_session
    from app.core.redis import get_redis
    from sqlalchemy import update as sql_update

    new_key = generate_api_key()
    old_key = current_user.api_key

    async with get_db_session() as session:
        await session.execute(
            sql_update(User).where(User.id == current_user.id).values(api_key=new_key)
        )
        await session.commit()

    # Bust old cache entries
    redis = get_redis()
    await redis.delete(f"auth:tier:{old_key}")
    # Store reverse lookup for new key
    await redis.set(f"auth:user_api_key:{current_user.id}", new_key, ex=300)
    await redis.set(f"auth:tier:{new_key}", current_user.tier.value, ex=300)

    return {"api_key": new_key, "message": "Old API key has been invalidated."}


# ── POST /auth/checkout ───────────────────────────────────────────────────────

@router.post("/checkout", response_model=dict)
async def create_checkout(
    body: CheckoutRequest,
    current_user: User = Depends(get_current_user),
    _rl: None = Depends(redis_rate_limit),
) -> Any:
    """Create a Stripe Checkout Session for the Pro upgrade."""
    from app.services.payment_service import create_checkout_session

    if current_user.tier.value in ("pro", "custom"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You are already on a Pro or Custom plan.",
        )

    return await create_checkout_session(
        user_id=current_user.id,
        user_email=current_user.email,
        stripe_customer_id=current_user.stripe_customer_id,
        success_url=body.success_url,
        cancel_url=body.cancel_url,
    )
