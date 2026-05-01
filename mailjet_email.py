"""
Send email via Mailjet. Uses ``hkex/.env`` (see ``env_loader``).
"""

from __future__ import annotations

import os
from typing import List, Optional

from mailjet_rest import Client

from env_loader import load_hkex_dotenv

load_hkex_dotenv()


def send_email(
    to_email: str,
    subject: str,
    html_content: str,
    from_email: Optional[str] = None,
    from_name: Optional[str] = None,
    bcc_emails: Optional[List[str]] = None,
) -> bool:
    api_key = os.getenv("MAILJET_API_KEY") or os.getenv("MJ_APIKEY_PUBLIC")
    api_secret = os.getenv("MAILJET_API_SECRET") or os.getenv("MJ_APIKEY_PRIVATE")

    if not api_key or not api_secret:
        print("ERROR: Mailjet API credentials not found in environment variables")
        print("  Looking for: MAILJET_API_KEY or MJ_APIKEY_PUBLIC")
        print("  Looking for: MAILJET_API_SECRET or MJ_APIKEY_PRIVATE")
        return False

    if from_email is None:
        from_email = os.getenv("MAILJET_FROM_EMAIL", "noreply@example.com")

    if from_name is None:
        from_name = os.getenv("MAILJET_FROM_NAME", "Newsletter")

    mailjet = Client(auth=(api_key, api_secret), version="v3.1")

    message = {
        "From": {"Email": from_email, "Name": from_name},
        "To": [{"Email": to_email, "Name": ""}],
        "Subject": subject,
        "HTMLPart": html_content,
    }

    if bcc_emails:
        message["Bcc"] = [{"Email": email, "Name": ""} for email in bcc_emails]

    data = {"Messages": [message]}

    try:
        result = mailjet.send.create(data=data)
        if result.status_code == 200:
            bcc_count = len(bcc_emails) if bcc_emails else 0
            if bcc_count > 0:
                print(f"Email sent successfully to {to_email} with {bcc_count} BCC recipient(s)")
            else:
                print(f"Email sent successfully to {to_email}")
            return True
        print(f"ERROR: Failed to send email. Status code: {result.status_code}")
        print(f"Response: {result.json()}")
        return False
    except Exception as e:
        print(f"ERROR: Exception while sending email: {e}")
        return False
