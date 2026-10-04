import logging
import json
import re

import requests
from flask import current_app, jsonify

from app.services.openai_service import generate_clinic_response


def log_http_response(response):
    logging.info("WhatsApp API status: %s", response.status_code)
    logging.info("WhatsApp API content-type: %s", response.headers.get("content-type"))
    logging.info("WhatsApp API body: %s", response.text)


def get_text_message_input(recipient, text):
    """Build a WhatsApp text payload."""
    has_url = bool(re.search(r"https?://\S+", text))
    return json.dumps(
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient,
            "type": "text",
            "text": {"preview_url": has_url, "body": text},
        }
    )


def send_message(data):
    """Send a message through the WhatsApp Cloud API."""
    headers = {
        "Content-type": "application/json",
        "Authorization": f"Bearer {current_app.config['ACCESS_TOKEN']}",
    }

    url = f"https://graph.facebook.com/{current_app.config['VERSION']}/{current_app.config['PHONE_NUMBER_ID']}/messages"

    try:
        response = requests.post(
            url, data=data, headers=headers, timeout=10
        )  # 10 seconds timeout as an example
        response.raise_for_status()  # Raises an HTTPError if the HTTP request returned an unsuccessful status code
    except requests.Timeout:
        logging.error("Timeout occurred while sending message")
        return jsonify({"status": "error", "message": "Request timed out"}), 408
    except (
        requests.RequestException
    ) as e:  # This will catch any general request exception
        logging.error(f"Request failed due to: {e}")
        return jsonify({"status": "error", "message": "Failed to send message"}), 500
    else:
        # Process the response as normal
        log_http_response(response)
        return response


def process_text_for_whatsapp(text):
    # Remove brackets
    pattern = r"\【.*?\】"
    # Substitute the pattern with an empty string
    text = re.sub(pattern, "", text).strip()

    # Pattern to find double asterisks including the word(s) in between
    pattern = r"\*\*(.*?)\*\*"

    # Replacement pattern with single asterisks
    replacement = r"*\1*"

    # Substitute occurrences of the pattern with the replacement
    whatsapp_style_text = re.sub(pattern, replacement, text)

    return whatsapp_style_text


def process_whatsapp_message(body):
    """Process a WhatsApp inbound message and respond using the clinic assistant."""
    wa_id = body["entry"][0]["changes"][0]["value"]["contacts"][0]["wa_id"]
    name = body["entry"][0]["changes"][0]["value"]["contacts"][0]["profile"]["name"]
    message = body["entry"][0]["changes"][0]["value"]["messages"][0]
    message_type = message["type"]

    if message_type == "text":
        message_body = message["text"]["body"]
        response = generate_clinic_response(message_body, wa_id, name)
        clean_response = process_text_for_whatsapp(response)
        data = get_text_message_input(wa_id, clean_response)
        send_message(data)
    else:
        texto = (
            "Por ahora puedo ayudarte mejor por texto. "
            "Si deseas agendar, editar o cancelar una cita, escribeme el detalle por mensaje."
        )
        data = get_text_message_input(wa_id, texto)
        send_message(data)


def is_valid_whatsapp_message(body):
    """
    Check if the incoming webhook event has a valid WhatsApp message structure.
    """
    return (
        body.get("object")
        and body.get("entry")
        and body["entry"][0].get("changes")
        and body["entry"][0]["changes"][0].get("value")
        and body["entry"][0]["changes"][0]["value"].get("messages")
        and body["entry"][0]["changes"][0]["value"]["messages"][0]
    )
