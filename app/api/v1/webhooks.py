"""
app/api/v1/webhooks.py — Stripe webhook receiver.

Routes:
  POST /webhooks/stripe — receives and verifies Stripe events
"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.services.payment_service import handle_stripe_webhook

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])


@router.post(
    "/stripe",
    summary="Stripe webhook receiver",
    status_code=status.HTTP_200_OK,
)
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(..., alias="stripe-signature"),
) -> dict:
    """Receive and process Stripe webhook events.

    Verifies the ``stripe-signature`` header before processing.
    Returns ``{"received": true}`` on success.
    """
    # Read raw body — MUST NOT parse as JSON (signature covers raw bytes)
    payload = await request.body()

    try:
        from stripe import WebhookSignatureVerificationError
        event_type = await handle_stripe_webhook(payload, stripe_signature)
    except WebhookSignatureVerificationError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Stripe signature.",
        )
    except RuntimeError as exc:
        # Stripe not configured
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        )

    return {"received": True, "event_type": event_type}
