from . import email
from .email import send_mail, send_otp_email, send_password_reset_email

__all__ = [
    "email",
    "send_mail",
    "send_otp_email",
    "send_password_reset_email",
]
