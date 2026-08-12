from __future__ import annotations

import re
import requests


class WhatsAppError(RuntimeError):
    pass


def normalize_recipient(value: str) -> str:
    return re.sub(r"\D", "", str(value or ""))


def send_text_message(*, access_token: str, phone_number_id: str, recipient: str, body: str, api_version: str = "v26.0") -> dict:
    recipient = normalize_recipient(recipient)
    if not recipient:
        raise WhatsAppError("Recipient number is empty.")
    url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "text",
        "text": {"preview_url": False, "body": body},
    }
    response = requests.post(url, headers=headers, json=payload, timeout=45)
    try:
        data = response.json()
    except Exception:
        data = {}
    if not response.ok:
        message = data.get("error", {}).get("message") or response.text
        raise WhatsAppError(f"WhatsApp API {response.status_code}: {message[:1000]}")
    return data
