"""OpenAI orchestration for the WhatsApp Dental Clinic assistant."""

from __future__ import annotations

import json
import logging
import os
import shelve
import re
from datetime import datetime
from typing import Any, Callable, Dict, List
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from app.services.appointment_service import AppointmentManager
from app.services.clinic_profile import (
    CLINIC_ADDRESS,
    CLINIC_NAME,
    CLINIC_TIMEZONE,
    DOCTOR_REFERENCE,
    DOCTOR_NAME,
    FIRST_VISIT_DISCOUNT_PERCENT,
    calculate_service_price,
    get_service,
    get_business_hours_summary,
    get_services_summary,
    is_first_visit_discount_eligible,
)
from app.services.observability import get_openai_client, traceable_function


load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
USE_OPENROUTER = os.getenv("USE_OPENROUTER", "false").lower() in ("true", "1", "yes")

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
CONVERSATION_DB_PATH = os.getenv("CONVERSATION_DB_PATH", "data/conversations")
BOOKING_CONTEXT_DB_PATH = os.getenv("BOOKING_CONTEXT_DB_PATH", "data/booking_context")

if USE_OPENROUTER and OPENROUTER_API_KEY:
    api_key = OPENROUTER_API_KEY
    base_url = "https://openrouter.ai/api/v1"
else:
    api_key = OPENAI_API_KEY
    base_url = None

client = get_openai_client(api_key, base_url=base_url) if api_key else None
appointment_manager = AppointmentManager()


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_dental_services",
            "description": "Lista los tipos de cita disponibles en la clinica, su duracion estimada y su precio en dolares.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_availability",
            "description": "Verifica si el dentista esta disponible para un tipo de cita y horario exacto.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service_key": {
                        "type": "string",
                        "description": "Clave publica del tipo de cita, por ejemplo limpieza_rutina o consulta_valoracion.",
                    },
                    "start_at": {
                        "type": "string",
                        "description": "Fecha y hora local de la clinica. Preferir YYYY-MM-DD HH:MM, pero tambien admite expresiones como manana 10:00, jueves 15:30 o 2026-03-25 10:00.",
                    },
                },
                "required": ["service_key", "start_at"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "suggest_available_slots",
            "description": "Busca proximos espacios disponibles cuando el horario deseado no esta libre.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service_key": {
                        "type": "string",
                        "description": "Clave publica del tipo de cita.",
                    },
                    "preferred_date": {
                        "type": "string",
                        "description": "Fecha deseada. Puede ser YYYY-MM-DD o expresiones como manana, pasado manana o jueves.",
                    },
                    "preferred_time_of_day": {
                        "type": "string",
                        "enum": ["any", "morning", "afternoon"],
                        "description": "Preferencia horaria del paciente.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Cantidad maxima de sugerencias.",
                    },
                },
                "required": ["service_key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_patient_appointments",
            "description": "Consulta las citas activas de un paciente para poder confirmarlas, editarlas o cancelarlas.",
            "parameters": {
                "type": "object",
                "properties": {
                    "patient_phone": {
                        "type": "string",
                        "description": "Numero del paciente. Usa el numero de WhatsApp del contacto actual si no hay otro.",
                    },
                    "patient_name": {
                        "type": "string",
                        "description": "Nombre del paciente si sirve para refinar la busqueda.",
                    },
                },
                "required": ["patient_phone"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_appointment",
            "description": "Crea una cita nueva cuando ya estan confirmados el tipo de cita, el horario y el paciente.",
            "parameters": {
                "type": "object",
                "properties": {
                    "patient_name": {
                        "type": "string",
                        "description": "Nombre completo del paciente.",
                    },
                    "patient_phone": {
                        "type": "string",
                        "description": "Telefono del paciente.",
                    },
                    "service_key": {
                        "type": "string",
                        "description": "Clave publica del tipo de cita.",
                    },
                    "start_at": {
                        "type": "string",
                        "description": "Fecha y hora local de la clinica. Preferir YYYY-MM-DD HH:MM, pero tambien admite expresiones como manana 10:00 o jueves 15:30.",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Motivo breve, sintomas o notas complementarias.",
                    },
                },
                "required": ["patient_name", "patient_phone", "service_key", "start_at"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_appointment",
            "description": "Edita una cita existente para cambiar horario, tipo de cita o notas.",
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {
                        "type": "string",
                        "description": "Identificador de la cita a modificar.",
                    },
                    "start_at": {
                        "type": "string",
                        "description": "Nuevo horario. Preferir YYYY-MM-DD HH:MM, pero tambien admite expresiones como manana 10:00 o jueves 15:30.",
                    },
                    "service_key": {
                        "type": "string",
                        "description": "Nueva clave de servicio si cambia el tipo de cita.",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Notas actualizadas.",
                    },
                },
                "required": ["appointment_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_appointment",
            "description": "Cancela una cita existente dejando registro del motivo si el paciente lo comparte.",
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {
                        "type": "string",
                        "description": "Identificador de la cita a cancelar.",
                    },
                    "cancellation_reason": {
                        "type": "string",
                        "description": "Motivo de cancelacion si el paciente lo menciona.",
                    },
                },
                "required": ["appointment_id"],
            },
        },
    },
]

TOOL_HANDLERS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "list_dental_services": appointment_manager.list_dental_services,
    "check_availability": appointment_manager.check_availability,
    "suggest_available_slots": appointment_manager.suggest_available_slots,
    "get_patient_appointments": appointment_manager.get_patient_appointments,
    "create_appointment": appointment_manager.create_appointment,
    "update_appointment": appointment_manager.update_appointment,
    "cancel_appointment": appointment_manager.cancel_appointment,
}


@traceable_function(name="Generate Clinic Response", run_type="chain")
def generate_clinic_response(message_body: str, wa_id: str, patient_name: str) -> str:
    """Generate a WhatsApp-ready assistant response for the clinic."""
    if not client:
        logging.error("OPENAI_API_KEY is missing.")
        return (
            "No pude iniciar el asistente de agenda porque falta la configuracion de OpenAI. "
            "Revisa el archivo .env e intenta de nuevo."
        )

    try:
        deterministic_response = _try_handle_booking_flow(message_body, wa_id, patient_name)
        if deterministic_response:
            return deterministic_response

        history = _load_history(wa_id)
        messages = [
            {"role": "system", "content": _build_system_prompt(wa_id, patient_name)},
            *history,
            {"role": "user", "content": message_body},
        ]

        for _ in range(6):
            completion = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.6,
            )

            message = completion.choices[0].message
            message_dict = _message_to_dict(message)
            messages.append(message_dict)

            if not message.tool_calls:
                final_text = (message.content or "").strip()
                if not final_text:
                    final_text = (
                        "Puedo ayudarte a agendar, mover o cancelar una cita dental. "
                        "Dime el tipo de cita y el horario que te interesa."
                    )
                _save_history(wa_id, messages)
                return final_text

            for tool_call in message.tool_calls:
                tool_result = _execute_tool_call(tool_call)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(tool_result, ensure_ascii=True),
                    }
                )

        logging.warning("Tool call loop limit reached for wa_id=%s", wa_id)
        _save_history(wa_id, messages)
        return "No pude cerrar la gestion en este intento. Si deseas, dime de nuevo la fecha o la accion que necesitas."
    except Exception as exc:
        logging.exception("Failed to generate clinic response: %s", exc)
        return (
            "Tuve un problema momentaneo al gestionar la cita. "
            "Intenta nuevamente en unos segundos."
        )


def _build_system_prompt(wa_id: str, patient_name: str) -> str:
    """Build the system prompt used by the clinic assistant."""
    now = datetime.now(ZoneInfo(CLINIC_TIMEZONE)).strftime("%Y-%m-%d %H:%M")
    return (
        f"Eres la secretaria virtual de {CLINIC_NAME}. Atiendes por WhatsApp con un tono calido, "
        f"humano, claro y profesional. Nunca digas que eres una IA.\n"
        f"Fecha y hora local actual: {now}\n"
        f"Zona horaria de la clinica: {CLINIC_TIMEZONE}\n"
        f"Direccion de la clinica: {CLINIC_ADDRESS}\n"
        f"Paciente actual: {patient_name}\n"
        f"Numero de WhatsApp del paciente: {wa_id}\n"
        f"Referente medico: {DOCTOR_REFERENCE}\n\n"
        "Objetivos:\n"
        "- Agendar, mover, cancelar y consultar citas.\n"
        "- Verificar disponibilidad real antes de confirmar cualquier horario.\n"
        "- Guiar al paciente al tipo de cita adecuado si no sabe cual necesita.\n\n"
        "Reglas operativas:\n"
        "- Nunca inventes horarios ni disponibilidad; usa herramientas.\n"
        "- Puedes convertir referencias relativas como hoy, manana, pasado manana o jueves a fechas locales de la clinica antes de llamar herramientas.\n"
        "- Si el paciente pide una cita y no tienes fecha exacta, pregunta antes de reservar.\n"
        "- Antes de crear una cita debes tener el nombre completo de la persona que asistira, aunque ya conozcas el nombre del perfil de WhatsApp.\n"
        "- Antes de crear una cita debes pedir confirmacion explicita del paciente para agendarla.\n"
        "- Solo crees la cita cuando ya tengas nombre, servicio, horario y confirmacion explicita.\n"
        "- Si el horario pedido no esta libre, ofrece alternativas con suggest_available_slots.\n"
        "- Si el paciente quiere editar o cancelar una cita y no da el identificador, primero consulta sus citas activas.\n"
        "- Para procedimientos complejos como endodoncia, corona o extraccion, recuerda que la confirmacion clinica final la hace el dentista.\n"
        "- Si una herramienta no puede interpretar un horario, pide aclaracion; no digas que hay un problema tecnico salvo que la herramienta devuelva un fallo real del sistema.\n"
        "- Cuando el paciente pregunte por servicios, tambien puedes mencionar precio estimado en dolares y duracion.\n"
        f"- En marzo y abril hay {FIRST_VISIT_DISCOUNT_PERCENT}% de descuento si es la primera visita; valida ese dato antes de confirmar el precio final cuando aplique.\n"
        "- Si el paciente pregunta por ubicacion, usa la direccion exacta de la clinica.\n"
        "- Responde de forma breve, natural, amable y orientada a resolver. Normalmente entre 2 y 6 lineas.\n"
        "- Si el paciente no sabe que reservar, orientalo usando el catalogo de servicios.\n\n"
        f"Horario de la clinica:\n{get_business_hours_summary()}\n\n"
        f"Servicios disponibles:\n{get_services_summary()}"
    )


@traceable_function(name="OpenAI Tool Call", run_type="tool")
def _execute_tool_call(tool_call) -> Dict[str, Any]:
    """Execute a tool requested by the model."""
    handler = TOOL_HANDLERS.get(tool_call.function.name)
    if not handler:
        return {"status": "error", "message": f"Herramienta no soportada: {tool_call.function.name}"}

    try:
        arguments = json.loads(tool_call.function.arguments or "{}")
    except json.JSONDecodeError:
        arguments = {}

    logging.info("Tool call requested: %s args=%s", tool_call.function.name, arguments)
    result = handler(**arguments)
    logging.info(
        "Tool call result: %s status=%s message=%s",
        tool_call.function.name,
        result.get("status"),
        result.get("message"),
    )
    return result


def _try_handle_booking_flow(message_body: str, wa_id: str, patient_name: str) -> str:
    """Handle common scheduling turns deterministically before using the LLM."""
    normalized_message = _normalize_text(message_body)
    context = _load_booking_context(wa_id)
    extracted_patient_name = _extract_patient_name(message_body, awaiting_name=bool(context.get("awaiting_patient_name")))
    if extracted_patient_name:
        context["patient_name"] = extracted_patient_name
    service_key = appointment_manager.find_service_key_from_text(message_body) or context.get("service_key")
    exact_datetime = appointment_manager.parse_datetime_input(message_body)
    date_value = appointment_manager.parse_date_input(message_body)
    time_of_day = appointment_manager.infer_time_of_day(message_body)

    logging.info(
        "Deterministic booking flow wa_id=%s service_key=%s exact_datetime=%s date_value=%s time_of_day=%s context=%s",
        wa_id,
        service_key,
        exact_datetime.isoformat() if exact_datetime else None,
        date_value.isoformat() if date_value else None,
        time_of_day,
        context,
    )

    if context.get("awaiting_patient_name"):
        logging.info("Deterministic booking flow branch=awaiting_patient_name")
        if not extracted_patient_name:
            return (
                "Para dejar la cita a nombre correcto, comparteme por favor el nombre completo "
                "de la persona que asistira."
            )

        if context.get("pending_start_at") and context.get("service_key"):
            return _route_pricing_and_consent_step(
                wa_id=wa_id,
                patient_name=extracted_patient_name,
                service_key=context["service_key"],
                start_at=context["pending_start_at"],
                notes=context.get("last_request", ""),
            )

    if context.get("awaiting_first_visit"):
        logging.info("Deterministic booking flow branch=awaiting_first_visit")
        if _is_affirmative_confirmation(normalized_message):
            return _request_booking_consent(
                wa_id=wa_id,
                patient_name=context.get("patient_name", ""),
                service_key=context.get("service_key", ""),
                start_at=context.get("pending_start_at", ""),
                notes=context.get("last_request", ""),
                is_first_visit=True,
            )
        if _is_negative_confirmation(normalized_message):
            return _request_booking_consent(
                wa_id=wa_id,
                patient_name=context.get("patient_name", ""),
                service_key=context.get("service_key", ""),
                start_at=context.get("pending_start_at", ""),
                notes=context.get("last_request", ""),
                is_first_visit=False,
            )
        return (
            "Para calcular el precio final, necesito confirmar si sera tu primera visita en la clinica. "
            "Responde si o no, por favor."
        )

    if context.get("awaiting_confirmation"):
        logging.info("Deterministic booking flow branch=awaiting_confirmation")
        if _is_negative_confirmation(normalized_message):
            preserved_context = {
                "service_key": context.get("service_key", ""),
                "preferred_date": context.get("preferred_date", ""),
                "preferred_time_of_day": context.get("preferred_time_of_day", "any"),
                "last_request": context.get("last_request", ""),
            }
            if context.get("patient_name"):
                preserved_context["patient_name"] = context["patient_name"]
            _save_booking_context(wa_id, preserved_context)
            return (
                "Perfecto, no la agendo todavia. "
                "Si quieres, puedo buscarte otro horario o ayudarte con otra fecha."
            )

        if _is_affirmative_confirmation(normalized_message):
            if context.get("pending_start_at") and context.get("service_key") and context.get("patient_name"):
                return _create_booking_from_datetime(
                    wa_id=wa_id,
                    patient_name=context["patient_name"],
                    service_key=context["service_key"],
                    start_at=context["pending_start_at"],
                    notes=_append_consent_note(context.get("last_request", "")),
                )
            return "Necesito confirmar nuevamente el nombre, el servicio y el horario antes de agendar."

    if _is_calendar_link_request(normalized_message):
        logging.info("Deterministic booking flow branch=calendar_link_request")
        appointments_result = appointment_manager.get_patient_appointments(patient_phone=wa_id, patient_name=patient_name)
        if appointments_result.get("status") == "success" and appointments_result.get("appointments"):
            appointment = appointments_result["appointments"][0]
            link = appointment.get("add_to_calendar_link") or appointment.get("calendar_link")
            if link:
                return (
                    "Claro. Aqui tienes el enlace para agregar tu cita al calendario:\n"
                    f"{link}"
                )
        return "No encontre una cita activa para compartirte el enlace del calendario en este momento."

    if _is_other_slots_request(normalized_message) and service_key and context.get("preferred_date"):
        logging.info("Deterministic booking flow branch=other_slots")
        suggestions = appointment_manager.suggest_available_slots(
            service_key=service_key,
            preferred_date=context.get("preferred_date"),
            preferred_time_of_day=context.get("preferred_time_of_day", "any"),
            limit=3,
        )
        return _handle_suggestion_result(wa_id, service_key, suggestions, context)

    if _is_booking_request(normalized_message) and service_key:
        logging.info("Deterministic booking flow branch=booking_request")
        if exact_datetime:
            logging.info("Deterministic booking flow branch=exact_datetime")
            availability = appointment_manager.check_availability(
                service_key=service_key,
                start_at=exact_datetime.strftime("%Y-%m-%d %H:%M"),
            )
            if availability.get("status") == "available":
                return _prepare_booking_confirmation(
                    wa_id=wa_id,
                    patient_name=context.get("patient_name", ""),
                    service_key=service_key,
                    start_at=exact_datetime.strftime("%Y-%m-%d %H:%M"),
                    notes=message_body,
                )
            if availability.get("status") == "unavailable":
                suggestions = appointment_manager.suggest_available_slots(
                    service_key=service_key,
                    preferred_date=exact_datetime.strftime("%Y-%m-%d"),
                    preferred_time_of_day=appointment_manager.infer_time_of_day(message_body),
                    limit=3,
                )
                return _handle_suggestion_result(
                    wa_id,
                    service_key,
                    suggestions,
                    {
                        "preferred_date": exact_datetime.strftime("%Y-%m-%d"),
                        "preferred_time_of_day": time_of_day,
                        "last_request": message_body,
                    },
                    unavailable_message=availability.get("message"),
                )
            return None

        if date_value:
            logging.info("Deterministic booking flow branch=date_only")
            suggestions = appointment_manager.suggest_available_slots(
                service_key=service_key,
                preferred_date=date_value.strftime("%Y-%m-%d"),
                preferred_time_of_day=time_of_day,
                limit=3,
            )
            return _handle_suggestion_result(
                wa_id,
                service_key,
                suggestions,
                {
                    "preferred_date": date_value.strftime("%Y-%m-%d"),
                    "preferred_time_of_day": time_of_day,
                    "last_request": message_body,
                },
            )

    if context.get("service_key") and context.get("preferred_date") and _looks_like_time_selection(normalized_message):
        logging.info("Deterministic booking flow branch=time_selection")
        selected_datetime = appointment_manager.parse_datetime_input(
            f"{context['preferred_date']} {message_body}"
        ) or appointment_manager.parse_datetime_input(
            f"{context['preferred_date']} {context.get('selected_time_hint', '')} {message_body}"
        )
        if selected_datetime:
            return _prepare_booking_confirmation(
                wa_id=wa_id,
                patient_name=context.get("patient_name", ""),
                service_key=context["service_key"],
                start_at=selected_datetime.strftime("%Y-%m-%d %H:%M"),
                notes=context.get("last_request", ""),
            )

    return ""


def _prepare_booking_confirmation(
    wa_id: str,
    patient_name: str,
    service_key: str,
    start_at: str,
    notes: str = "",
) -> str:
    clean_name = patient_name.strip()
    if not clean_name:
        _save_booking_context(
            wa_id,
            {
                "service_key": service_key,
                "pending_start_at": start_at,
                "preferred_date": start_at.split(" ")[0],
                "preferred_time_of_day": "any",
                "last_request": notes,
                "awaiting_patient_name": "1",
            },
        )
        service = get_service(service_key)
        service_name = service.name if service else "la cita"
        return (
            f"Tengo disponible ese espacio para {service_name}. "
            "Antes de confirmarlo, comparteme por favor el nombre completo de la persona que asistira."
        )

    return _route_pricing_and_consent_step(wa_id, clean_name, service_key, start_at, notes)


def _route_pricing_and_consent_step(
    wa_id: str,
    patient_name: str,
    service_key: str,
    start_at: str,
    notes: str = "",
) -> str:
    start_dt = appointment_manager.parse_datetime_input(start_at)
    prior_visit_result = appointment_manager.has_prior_visit(
        patient_phone=wa_id,
        patient_name=patient_name,
        before_start_at=start_at,
    )
    has_prior_visit = prior_visit_result.get("status") == "success" and prior_visit_result.get("has_prior_visit", False)

    if start_dt and is_first_visit_discount_eligible(start_dt.date()) and not has_prior_visit:
        existing_context = _load_booking_context(wa_id)
        if existing_context.get("first_visit_status") not in {"yes", "no"}:
            _save_booking_context(
                wa_id,
                {
                    "service_key": service_key,
                    "patient_name": patient_name,
                    "pending_start_at": start_at,
                    "preferred_date": start_at.split(" ")[0],
                    "preferred_time_of_day": "any",
                    "last_request": notes,
                    "awaiting_first_visit": "1",
                },
            )
            return (
                f"Antes de confirmarte el precio final, necesito validar si esta sera tu primera visita en {CLINIC_NAME}. "
                f"En marzo y abril ofrecemos {FIRST_VISIT_DISCOUNT_PERCENT}% de descuento en la primera visita. "
                "Responde si o no, por favor."
            )

    first_visit = (
        not has_prior_visit
        and _load_booking_context(wa_id).get("first_visit_status") == "yes"
    )
    return _request_booking_consent(
        wa_id,
        patient_name,
        service_key,
        start_at,
        notes,
        is_first_visit=first_visit,
        has_prior_visit=has_prior_visit,
    )


def _request_booking_consent(
    wa_id: str,
    patient_name: str,
    service_key: str,
    start_at: str,
    notes: str = "",
    is_first_visit: bool = False,
    has_prior_visit: bool = False,
) -> str:
    service = get_service(service_key)
    start_dt = appointment_manager.parse_datetime_input(start_at)
    pricing = calculate_service_price(service, start_dt.date() if service and start_dt else None, is_first_visit) if service else None
    _save_booking_context(
        wa_id,
        {
            "service_key": service_key,
            "patient_name": patient_name,
            "pending_start_at": start_at,
            "preferred_date": start_at.split(" ")[0],
            "preferred_time_of_day": "any",
            "last_request": notes,
            "awaiting_confirmation": "1",
            "first_visit_status": "yes" if is_first_visit else "no",
        },
    )
    service_name = service.name if service else "la cita"
    if pricing and pricing["discount_percent"] > 0:
        price_text = (
            f"US${pricing['final_price_usd']:.2f} con {int(pricing['discount_percent'])}% de descuento "
            f"de primera visita sobre US${pricing['base_price_usd']:.2f}"
        )
    elif pricing:
        price_text = f"US${pricing['final_price_usd']:.2f}"
    else:
        price_text = "precio por confirmar"
    prior_visit_text = (
        " Ya revisamos tu historial y no aplica descuento de primera visita."
        if has_prior_visit
        else ""
    )
    return (
        f"Antes de agendar, te confirmo el detalle: {service_name} para {patient_name} el {start_at}. "
        f"Te atendera {DOCTOR_NAME}. Precio estimado: {price_text}. Direccion: {CLINIC_ADDRESS}. "
        f"{prior_visit_text} Si estas de acuerdo y autorizas agendarla, responde si. Si deseas cambiar algo, responde no."
    )


def _create_booking_from_datetime(
    wa_id: str,
    patient_name: str,
    service_key: str,
    start_at: str,
    notes: str = "",
) -> str:
    availability = appointment_manager.check_availability(service_key=service_key, start_at=start_at)
    if availability.get("status") != "available":
        if availability.get("status") == "unavailable":
            suggestions = appointment_manager.suggest_available_slots(
                service_key=service_key,
                preferred_date=start_at.split(" ")[0],
                preferred_time_of_day="any",
                limit=3,
            )
            return _handle_suggestion_result(
                wa_id,
                service_key,
                suggestions,
                {"preferred_date": start_at.split(" ")[0], "preferred_time_of_day": "any"},
                unavailable_message=availability.get("message"),
            )
        return ""

    created = appointment_manager.create_appointment(
        patient_name=patient_name,
        patient_phone=wa_id,
        service_key=service_key,
        start_at=start_at,
        notes=notes,
    )
    if created.get("status") != "success":
        return ""

    _clear_booking_context(wa_id)
    appointment = created["appointment"]
    service = get_service(service_key)
    start_dt = appointment_manager.parse_datetime_input(start_at)
    first_visit = _load_booking_context(wa_id).get("first_visit_status") == "yes"
    pricing = calculate_service_price(service, start_dt.date() if service and start_dt else None, first_visit) if service else None
    if pricing and pricing["discount_percent"] > 0:
        price_text = (
            f" El precio final es US${pricing['final_price_usd']:.2f}, "
            f"incluyendo {int(pricing['discount_percent'])}% de descuento de primera visita."
        )
    elif pricing:
        price_text = f" El precio estimado es US${pricing['final_price_usd']:.2f}."
    else:
        price_text = ""
    link = appointment.get("add_to_calendar_link") or appointment.get("calendar_link") or ""
    link_text = (
        f" Si deseas agregarla a tu calendario, aqui tienes el enlace:\n{link}"
        if link
        else ""
    )
    return (
        f"Perfecto, {patient_name}, tu cita quedo agendada para {appointment['start_at_display']} "
        f"por {appointment['service_name']} con {DOCTOR_NAME}.{price_text} "
        f"Te esperamos en {CLINIC_ADDRESS}.{link_text} Si luego deseas cambiarla o cancelarla, solo escribeme."
    )


def _handle_suggestion_result(
    wa_id: str,
    service_key: str,
    suggestions: Dict[str, Any],
    context_updates: Dict[str, str],
    unavailable_message: str = "",
) -> str:
    if suggestions.get("status") != "success":
        return ""

    previous_context = _load_booking_context(wa_id)
    slots = suggestions.get("suggestions", [])
    context_payload = {
        "service_key": service_key,
        "preferred_date": context_updates.get("preferred_date", ""),
        "preferred_time_of_day": context_updates.get("preferred_time_of_day", "any"),
        "last_request": context_updates.get("last_request", ""),
    }
    if previous_context.get("patient_name"):
        context_payload["patient_name"] = previous_context["patient_name"]
    _save_booking_context(wa_id, context_payload)

    if not slots:
        return (
            "No encontre espacios libres con esa referencia. "
            "Si quieres, te propongo otro dia o una franja distinta."
        )

    slot_lines = [slot["start_at_display"] for slot in slots]
    prefix = "Ese horario no esta libre. " if unavailable_message else ""
    return (
        f"{prefix}Te puedo ofrecer estas opciones: "
        + ", ".join(slot_lines)
        + ". Si una te funciona, dime por ejemplo: 'me quedo con la de las 8'."
    )


def _load_booking_context(wa_id: str) -> Dict[str, str]:
    with shelve.open(BOOKING_CONTEXT_DB_PATH) as memory:
        return memory.get(wa_id, {})


def _save_booking_context(wa_id: str, context: Dict[str, str]) -> None:
    with shelve.open(BOOKING_CONTEXT_DB_PATH, writeback=True) as memory:
        memory[wa_id] = context


def _clear_booking_context(wa_id: str) -> None:
    with shelve.open(BOOKING_CONTEXT_DB_PATH, writeback=True) as memory:
        if wa_id in memory:
            del memory[wa_id]


def _normalize_text(value: str) -> str:
    value = value.lower().strip()
    value = value.replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u")
    value = re.sub(r"\s+", " ", value)
    return value


def _is_booking_request(normalized_message: str) -> bool:
    keywords = (
        "agendar",
        "reservar",
        "cita",
        "quiero una cita",
        "me gustaria una cita",
        "me gustaria agendar",
        "quiero agendar",
    )
    return any(keyword in normalized_message for keyword in keywords)


def _is_other_slots_request(normalized_message: str) -> bool:
    keywords = (
        "otros horarios",
        "otras opciones",
        "otra hora",
        "otro horario",
        "que opciones hay",
    )
    return any(keyword in normalized_message for keyword in keywords)


def _is_calendar_link_request(normalized_message: str) -> bool:
    keywords = (
        "link",
        "enlace",
        "calendario",
        "agregar a mi calendario",
        "agregarlo a mi calendario",
        "mandame el link",
        "envia el link",
        "enviame el link",
    )
    return any(keyword in normalized_message for keyword in keywords)


def _is_affirmative_confirmation(normalized_message: str) -> bool:
    confirmations = {
        "si",
        "sí",
        "confirmo",
        "autorizo",
        "de acuerdo",
        "esta bien",
        "está bien",
        "adelante",
        "ok",
        "okay",
    }
    return normalized_message in confirmations or normalized_message.startswith("si ")


def _is_negative_confirmation(normalized_message: str) -> bool:
    rejections = {
        "no",
        "no todavia",
        "todavia no",
        "aun no",
        "aún no",
        "cancelar",
        "mejor no",
    }
    return normalized_message in rejections or normalized_message.startswith("no ")


def _append_consent_note(notes: str) -> str:
    consent_timestamp = datetime.now(ZoneInfo(CLINIC_TIMEZONE)).strftime("%Y-%m-%d %H:%M")
    consent_note = f"Consentimiento de agenda confirmado por WhatsApp el {consent_timestamp}."
    clean_notes = notes.strip()
    if not clean_notes:
        return consent_note
    return f"{clean_notes}\n{consent_note}"


def _looks_like_time_selection(normalized_message: str) -> bool:
    return any(
        marker in normalized_message
        for marker in ("a las", "las 8", "las 9", "las 10", "esta bien", "me quedo con", "esa me sirve")
    )


def _extract_patient_name(message_body: str, awaiting_name: bool = False) -> str:
    value = re.sub(r"\s+", " ", message_body).strip(" .,:;!-")
    if not value:
        return ""

    patterns = (
        r"(?:mi nombre es|soy)\s+([A-Za-zÁÉÍÓÚáéíóúÑñ ]{3,60})$",
        r"(?:a nombre de|seria para|sería para|es para)\s+([A-Za-zÁÉÍÓÚáéíóúÑñ ]{3,60})$",
    )
    for pattern in patterns:
        match = re.search(pattern, value, re.IGNORECASE)
        if match:
            return _clean_patient_name(match.group(1))

    if not awaiting_name:
        return ""

    if re.search(r"\d", value):
        return ""

    normalized = _normalize_text(value)
    blocked_terms = (
        "otros horarios",
        "otra opcion",
        "otro horario",
        "manana",
        "tarde",
        "hoy",
        "pasado manana",
        "a las",
        "agendar",
        "cita",
    )
    if any(term in normalized for term in blocked_terms):
        return ""

    return _clean_patient_name(value)


def _clean_patient_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-zÁÉÍÓÚáéíóúÑñ ]+", " ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned.split()) < 2:
        return ""
    return " ".join(word.capitalize() for word in cleaned.split())


def _message_to_dict(message) -> Dict[str, Any]:
    """Normalize OpenAI message objects into plain dictionaries."""
    if isinstance(message, dict):
        return message
    if hasattr(message, "model_dump"):
        return message.model_dump(exclude_none=True)

    payload: Dict[str, Any] = {
        "role": getattr(message, "role", None),
        "content": getattr(message, "content", None),
    }
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        payload["tool_calls"] = tool_calls
    return payload


def _load_history(wa_id: str) -> List[Dict[str, Any]]:
    """Load the stored user/assistant conversation history for a contact."""
    with shelve.open(CONVERSATION_DB_PATH) as memory:
        return memory.get(wa_id, [])


def _save_history(wa_id: str, messages: List[Dict[str, Any]]) -> None:
    """Persist only the clean user/assistant history for future turns."""
    compact_history: List[Dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if role not in {"user", "assistant"}:
            continue
        if role == "assistant" and message.get("tool_calls"):
            continue
        if not content:
            continue
        compact_history.append({"role": role, "content": content})

    with shelve.open(CONVERSATION_DB_PATH, writeback=True) as memory:
        memory[wa_id] = compact_history[-16:]
