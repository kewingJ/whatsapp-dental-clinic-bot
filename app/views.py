import logging
import json
from datetime import datetime, timezone

from flask import Blueprint, Response, current_app, jsonify, request

from .decorators.security import signature_required
from .services.appointment_service import AppointmentManager
from .services.clinic_profile import CLINIC_ADDRESS, CLINIC_NAME, DOCTOR_NAME
from .utils.whatsapp_utils import (
    process_whatsapp_message,
    is_valid_whatsapp_message,
)

webhook_blueprint = Blueprint("webhook", __name__)
appointment_manager = AppointmentManager()


def handle_message():
    """
    Handle incoming webhook events from the WhatsApp API.

    This function processes incoming WhatsApp messages and other events,
    such as delivery statuses. If the event is a valid message, it gets
    processed. If the incoming payload is not a recognized WhatsApp event,
    an error is returned.

    Every message send will trigger 4 HTTP requests to your webhook: message, sent, delivered, read.

    Returns:
        response: A tuple containing a JSON response and an HTTP status code.
    """
    body = request.get_json() or {}

    if (
        body.get("entry", [{}])[0]
        .get("changes", [{}])[0]
        .get("value", {})
        .get("statuses")
    ):
        logging.info("Received a WhatsApp status update.")
        return jsonify({"status": "ok"}), 200

    try:
        if is_valid_whatsapp_message(body):
            process_whatsapp_message(body)
            return jsonify({"status": "ok"}), 200
        else:
            # if the request is not a WhatsApp API event, return an error
            return (
                jsonify({"status": "error", "message": "Not a WhatsApp API event"}),
                404,
            )
    except json.JSONDecodeError:
        logging.error("Failed to decode JSON")
        return jsonify({"status": "error", "message": "Invalid JSON provided"}), 400


# Required webhook verifictaion for WhatsApp
def verify():
    # Parse params from the webhook verification request
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    # Check if a token and mode were sent
    if mode and token:
        # Check the mode and token sent are correct
        if mode == "subscribe" and token == current_app.config["VERIFY_TOKEN"]:
            # Respond with 200 OK and challenge token from the request
            logging.info("WEBHOOK_VERIFIED")
            return challenge, 200
        else:
            # Responds with '403 Forbidden' if verify tokens do not match
            logging.info("VERIFICATION_FAILED")
            return jsonify({"status": "error", "message": "Verification failed"}), 403
    else:
        # Responds with '400 Bad Request' if verify tokens do not match
        logging.info("MISSING_PARAMETER")
        return jsonify({"status": "error", "message": "Missing parameters"}), 400


@webhook_blueprint.route("/webhook", methods=["GET"])
def webhook_get():
    return verify()


@webhook_blueprint.route("/webhook", methods=["POST"])
@signature_required
def webhook_post():
    return handle_message()


@webhook_blueprint.route("/calendar/<appointment_id>.ics", methods=["GET"])
def appointment_calendar_file(appointment_id: str):
    appointment = appointment_manager.get_appointment_by_id(appointment_id)
    if not appointment or appointment.get("status") == "cancelled":
        return jsonify({"status": "error", "message": "Appointment not found"}), 404

    calendar_body = _build_ics_content(appointment)
    filename = f"{appointment_id}.ics"
    return Response(
        calendar_body,
        mimetype="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


def _build_ics_content(appointment: dict) -> str:
    start_dt = datetime.fromisoformat(appointment["start_at"]).astimezone(timezone.utc)
    end_dt = datetime.fromisoformat(appointment["end_at"]).astimezone(timezone.utc)
    now_dt = datetime.now(timezone.utc)

    def _escape(value: str) -> str:
        return (
            str(value)
            .replace("\\", "\\\\")
            .replace(";", r"\;")
            .replace(",", r"\,")
            .replace("\n", r"\n")
        )

    summary = _escape(appointment.get("service_name", "Dental Appointment"))
    description = _escape(
        f"{CLINIC_NAME}\n"
        f"Patient: {appointment.get('patient_name', '')}\n"
        f"Service: {appointment.get('service_name', '')}\n"
        f"Doctor: {DOCTOR_NAME}"
    )
    location = _escape(CLINIC_ADDRESS)

    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        f"PRODID:-//{CLINIC_NAME}//Appointment Calendar//ES\r\n"
        "CALSCALE:GREGORIAN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{appointment.get('appointment_id', '')}@dental-clinic-bot\r\n"
        f"DTSTAMP:{now_dt.strftime('%Y%m%dT%H%M%SZ')}\r\n"
        f"DTSTART:{start_dt.strftime('%Y%m%dT%H%M%SZ')}\r\n"
        f"DTEND:{end_dt.strftime('%Y%m%dT%H%M%SZ')}\r\n"
        f"SUMMARY:{summary}\r\n"
        f"DESCRIPTION:{description}\r\n"
        f"LOCATION:{location}\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
