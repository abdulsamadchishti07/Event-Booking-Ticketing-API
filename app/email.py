import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from .config import settings

logger = logging.getLogger(__name__)

# Configure Jinja2 environment to load email HTML templates
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
jinja_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=True
)


def send_mail(to_email: str, subject: str, html_content: str) -> None:
    """
    Sends an HTML email to a specified recipient using SMTP with STARTTLS.

    Args:
        to_email: Destination recipient email address.
        subject: Email subject line.
        html_content: Rendered HTML body of the email message.
    """
    try:
        # Build multipart message (supporting HTML content)
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = settings.default_from_email
        msg["To"] = to_email

        # Attach rendered HTML body
        part = MIMEText(html_content, "html")
        msg.attach(part)

        # Sanitize credentials (strip spaces and whitespace)
        username = settings.email_host_user.strip()
        password = settings.email_host_password.replace(" ", "").strip()

        # Connect to SMTP server, upgrade to TLS, authenticate, and send
        with smtplib.SMTP(settings.email_smtp_server, settings.email_smtp_port) as server:
            server.starttls()
            server.login(username, password)
            server.sendmail(settings.default_from_email, to_email, msg.as_string())

        logger.info(f"Email successfully sent to {to_email}")

    except Exception as e:
        logger.error(f"Failed to send email to {to_email}: {e}")


def send_otp_email(to_email: str, otp: str) -> None:
    """
    Renders the OTP verification HTML template and sends it to the user.

    Args:
        to_email: Destination user email address.
        otp: 6-digit numeric verification code.
    """
    subject = "Verify your Account - Event Booking"
    template = jinja_env.get_template("verify_email.html")
    html_content = template.render(otp=otp)

    send_mail(to_email=to_email, subject=subject, html_content=html_content)



def send_password_reset_email(to_email: str, otp: str) -> None:
    """
    Sends an HTML email with the 6-digit password reset OTP.
    """
    subject = "Password Reset Code - Event Booking"
    html_content = f"""
    <div style="font-family: Arial, sans-serif; max-width: 500px; margin: auto; padding: 24px; border: 1px solid #e2e8f0; border-radius: 12px; background: #ffffff;">
        <h2 style="color: #4f46e5; margin-top: 0;">🎟️ Password Reset Request</h2>
        <p style="color: #334155; line-height: 1.5;">You requested to reset your password. Use the verification code below to set a new password:</p>
        <div style="font-size: 32px; font-weight: 800; letter-spacing: 8px; color: #4f46e5; text-align: center; padding: 18px; margin: 20px 0; background: #f1f5f9; border: 2px dashed #818cf8; border-radius: 8px;">
            {otp}
        </div>
        <p style="color: #64748b; font-size: 13px;">This code will expire in <strong>5 minutes</strong>. If you did not request this, please ignore this email.</p>
    </div>
    """
    send_mail(to_email=to_email, subject=subject, html_content=html_content)
