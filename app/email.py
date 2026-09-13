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
