"""
Background task definitions and handlers.
Kept separate from worker execution to ensure import safety without side effects (e.g. signal handlers).
"""

from app.services import email as email_service

TASK_HANDLERS = {
    "send_otp_email": lambda payload: email_service.send_otp_email(
        to_email=payload["to_email"],
        otp=payload["otp"]
    ),
    "send_password_reset_email": lambda payload: email_service.send_password_reset_email(
        to_email=payload["to_email"],
        otp=payload["otp"]
    ),
    "send_invoice_email": lambda payload: email_service.send_invoice_email(
        to_email=payload["to_email"],
        user_name=payload.get("user_name", "Customer"),
        invoice_number=payload.get("invoice_number", f"INV-{payload.get('booking_id')}"),
        event_name=payload.get("event_name", "Event"),
        quantity=payload.get("quantity", 1),
        total_amount=str(payload.get("total_amount", "0.00")),
        booking_id=payload.get("booking_id", 0)
    ),
    "send_cancellation_email": lambda payload: email_service.send_cancellation_email(
        to_email=payload["to_email"],
        user_name=payload.get("user_name", "Customer"),
        event_name=payload.get("event_name", "Event"),
        refund_amount=str(payload.get("refund_amount", "0.00")),
        reason=payload.get("reason", "Cancelled by customer"),
        booking_id=payload.get("booking_id", 0)
    ),
}
