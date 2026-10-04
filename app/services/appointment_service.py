"""Appointment management layer for the dental clinic assistant."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import unicodedata
import uuid
from urllib.parse import urlencode
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from app.services.clinic_profile import (
    BREAK_WINDOWS,
    CLINIC_ADDRESS,
    CLINIC_NAME,
    CLINIC_TIMEZONE,
    DOCTOR_NAME,
    SLOT_INTERVAL_MINUTES,
    WEEKDAY_LABELS,
    WORKING_HOURS,
    get_service,
    get_service_catalog,
    serialize_service,
)
from app.services.google_calendar_service import GoogleCalendarService


_STORAGE_LOCK = threading.Lock()

WEEKDAY_NAME_TO_INDEX = {
    "lunes": 0,
    "martes": 1,
    "miercoles": 2,
    "miércoles": 2,
    "jueves": 3,
    "viernes": 4,
    "sabado": 5,
    "sábado": 5,
    "domingo": 6,
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

SERVICE_KEYWORDS = {
    "consulta_valoracion": [
        "valoracion",
        "valoración",
        "consulta",
        "revision",
        "revisión",
        "chequeo",
    ],
    "limpieza_rutina": [
        "limpieza",
        "limpieza dental",
        "profilaxis",
    ],
    "limpieza_profunda": [
        "limpieza profunda",
        "deep cleaning",
        "periodontal",
    ],
    "resina_dental": [
        "resina",
        "obturacion",
        "obturación",
        "relleno",
        "empaste",
    ],
    "extraccion_simple": [
        "extraccion",
        "extracción",
        "sacarme una muela",
        "sacar una muela",
    ],
    "endodoncia": [
        "endodoncia",
        "conducto",
        "tratamiento de conducto",
    ],
    "corona_dental": [
        "corona",
        "corona dental",
    ],
    "blanqueamiento": [
        "blanqueamiento",
        "blanqueamiento dental",
        "whitening",
    ],
    "urgencia_dental": [
        "urgencia",
        "emergencia",
        "dolor fuerte",
        "me duele",
    ],
    "control_post_tratamiento": [
        "control",
        "revision de control",
        "seguimiento",
    ],
}


@dataclass
class Appointment:
    """Stored appointment representation."""

    appointment_id: str
    patient_name: str
    patient_phone: str
    service_key: str
    service_name: str
    start_at: str
    end_at: str
    notes: str
    status: str
    created_at: str
    updated_at: str
    cancellation_reason: str = ""


class AppointmentManager:
    """Handles persistence, availability checks and appointment CRUD."""

    def __init__(self, storage_path: str = "data/appointments.json"):
        self.storage_path = Path(storage_path)
        self.timezone = ZoneInfo(CLINIC_TIMEZONE)
        self.google_calendar = GoogleCalendarService(timezone=CLINIC_TIMEZONE)
        self.google_calendar_requested = self.google_calendar.is_requested()
        self.google_calendar_ready = (
            self.google_calendar_requested and not self.google_calendar.get_configuration_error()
        )
        logging.info(
            "AppointmentManager initialized google_calendar_requested=%s google_calendar_ready=%s",
            self.google_calendar_requested,
            self.google_calendar_ready,
        )
        if not self.google_calendar_requested:
            self._ensure_storage()

    def list_dental_services(self) -> Dict[str, object]:
        """Expose the service catalog to the LLM."""
        payload = {
            "clinic_name": CLINIC_NAME,
            "timezone": CLINIC_TIMEZONE,
            "services": [serialize_service(service) for service in get_service_catalog()],
        }
        if self.google_calendar_requested:
            payload["scheduling_backend"] = "google_calendar"
        else:
            payload["scheduling_backend"] = "local_json"
        return payload

    def find_service_key_from_text(self, value: str) -> Optional[str]:
        """Infer a service key from natural-language text."""
        normalized = self._normalize_text(value)
        for service_key, keywords in SERVICE_KEYWORDS.items():
            for keyword in keywords:
                if self._normalize_text(keyword) in normalized:
                    return service_key
        return None

    def parse_datetime_input(self, value: Optional[str]) -> Optional[datetime]:
        """Public wrapper for datetime parsing from natural language."""
        return self._parse_datetime(value)

    def parse_date_input(self, value: Optional[str]) -> Optional[date]:
        """Public wrapper for date parsing from natural language."""
        return self._parse_date(value)

    def infer_time_of_day(self, value: Optional[str]) -> str:
        """Infer a coarse time-of-day preference from free text."""
        if not value:
            return "any"
        normalized = self._normalize_text(value)
        if any(token in normalized for token in ("temprano", "manana", "mañana", "morning")):
            return "morning"
        if any(token in normalized for token in ("tarde", "afternoon", "despues de las 12")):
            return "afternoon"
        return "any"

    def get_patient_appointments(
        self,
        patient_phone: str,
        patient_name: Optional[str] = None,
    ) -> Dict[str, object]:
        """Return scheduled appointments for a patient."""
        normalized_phone = self._normalize_phone(patient_phone)
        if not normalized_phone:
            return {"status": "error", "message": "Necesito un numero de telefono para buscar la cita."}

        backend_error = self._get_backend_error()
        if backend_error:
            return backend_error

        appointments = []
        if self.google_calendar_ready:
            for event in self.google_calendar.find_patient_events(normalized_phone, patient_name):
                appointments.append(self._serialize_appointment(event))
        else:
            for appointment in self._load_appointments():
                if appointment.status != "scheduled":
                    continue
                if appointment.patient_phone == normalized_phone:
                    appointments.append(self._serialize_appointment(appointment))
                    continue
                if patient_name and appointment.patient_name.lower() == patient_name.strip().lower():
                    appointments.append(self._serialize_appointment(appointment))

        appointments.sort(key=lambda item: item["start_at"])
        return {
            "status": "success",
            "count": len(appointments),
            "appointments": appointments,
        }

    def get_appointment_by_id(self, appointment_id: str) -> Optional[Dict[str, str]]:
        """Return a single appointment by identifier."""
        backend_error = self._get_backend_error()
        if backend_error:
            return None

        if self.google_calendar_ready:
            return self._get_google_appointment(appointment_id)

        appointment = next(
            (item for item in self._load_appointments() if item.appointment_id == appointment_id),
            None,
        )
        if not appointment:
            return None
        return self._serialize_appointment(appointment)

    def has_prior_visit(
        self,
        patient_phone: str,
        patient_name: Optional[str],
        before_start_at: str,
    ) -> Dict[str, object]:
        """Check whether the patient already had a prior non-cancelled appointment before the target date."""
        normalized_phone = self._normalize_phone(patient_phone)
        if not normalized_phone:
            return {"status": "error", "message": "Necesito un numero de telefono para revisar el historial."}

        target_dt = self._parse_datetime(before_start_at)
        if not target_dt:
            return {"status": "error", "message": "No pude interpretar la fecha para revisar el historial."}

        backend_error = self._get_backend_error()
        if backend_error:
            return backend_error

        if self.google_calendar_ready:
            prior_events = self.google_calendar.find_patient_events_before(
                patient_phone=normalized_phone,
                before_dt=target_dt,
                patient_name=patient_name,
            )
            return {
                "status": "success",
                "has_prior_visit": bool(prior_events),
                "count": len(prior_events),
            }

        normalized_name = (patient_name or "").strip().lower()
        matches = []
        for appointment in self._load_appointments():
            if appointment.status != "scheduled":
                continue
            appointment_start = datetime.fromisoformat(appointment.start_at)
            if appointment_start >= target_dt:
                continue
            if appointment.patient_phone == normalized_phone:
                matches.append(appointment)
                continue
            if normalized_name and appointment.patient_name.lower() == normalized_name:
                matches.append(appointment)

        return {
            "status": "success",
            "has_prior_visit": bool(matches),
            "count": len(matches),
        }

    def check_availability(self, service_key: str, start_at: str) -> Dict[str, object]:
        """Check if the dentist is available for a requested slot."""
        logging.info("AppointmentManager check_availability service_key=%s start_at=%s", service_key, start_at)
        service = get_service(service_key)
        if not service:
            return {"status": "error", "message": f"No reconozco el tipo de cita '{service_key}'."}

        start_dt = self._parse_datetime(start_at)
        if not start_dt:
            return {
                "status": "error",
                "message": "Formato de fecha invalido. Usa YYYY-MM-DD HH:MM.",
            }

        end_dt = start_dt + timedelta(minutes=service.duration_minutes)
        validation_error = self._validate_slot_window(start_dt, end_dt)
        if validation_error:
            return {
                "status": "unavailable",
                "available": False,
                "message": validation_error,
                "requested_slot": self._format_slot(start_dt, end_dt),
            }

        backend_error = self._get_backend_error()
        if backend_error:
            return backend_error

        conflict = self._find_conflict(start_dt, end_dt)
        if conflict:
            return {
                "status": "unavailable",
                "available": False,
                "message": "Ese horario ya esta ocupado.",
                "requested_slot": self._format_slot(start_dt, end_dt),
                "conflict": self._serialize_appointment(conflict),
            }

        return {
            "status": "available",
            "available": True,
            "message": "El horario esta disponible.",
            "requested_slot": self._format_slot(start_dt, end_dt),
            "service": serialize_service(service),
        }

    def suggest_available_slots(
        self,
        service_key: str,
        preferred_date: Optional[str] = None,
        preferred_time_of_day: str = "any",
        limit: int = 3,
    ) -> Dict[str, object]:
        """Find the next available slots for a given appointment type."""
        logging.info(
            "AppointmentManager suggest_available_slots service_key=%s preferred_date=%s preferred_time_of_day=%s limit=%s",
            service_key,
            preferred_date,
            preferred_time_of_day,
            limit,
        )
        service = get_service(service_key)
        if not service:
            return {"status": "error", "message": f"No reconozco el tipo de cita '{service_key}'."}

        limit = max(1, min(limit, 6))
        base_date = self._parse_date(preferred_date) or self._now().date()
        slots: List[Dict[str, str]] = []

        backend_error = self._get_backend_error()
        if backend_error:
            return backend_error

        for day_offset in range(14):
            current_date = base_date + timedelta(days=day_offset)
            weekday = current_date.weekday()
            if weekday not in WORKING_HOURS:
                continue

            for candidate_start in self._iter_day_slots(current_date, service.duration_minutes):
                if candidate_start < self._rounded_now():
                    continue
                if not self._matches_time_of_day(candidate_start, preferred_time_of_day):
                    continue

                candidate_end = candidate_start + timedelta(minutes=service.duration_minutes)
                if self._find_conflict(candidate_start, candidate_end):
                    continue

                slots.append(self._format_slot(candidate_start, candidate_end))
                if len(slots) >= limit:
                    return {
                        "status": "success",
                        "service": serialize_service(service),
                        "suggestions": slots,
                    }

        return {
            "status": "success",
            "service": serialize_service(service),
            "suggestions": slots,
            "message": "No encontre espacios libres en la ventana de busqueda configurada.",
        }

    def create_appointment(
        self,
        patient_name: str,
        patient_phone: str,
        service_key: str,
        start_at: str,
        notes: str = "",
    ) -> Dict[str, object]:
        """Create a new appointment if the slot is valid and free."""
        logging.info(
            "AppointmentManager create_appointment patient_name=%s patient_phone=%s service_key=%s start_at=%s",
            patient_name,
            patient_phone,
            service_key,
            start_at,
        )
        service = get_service(service_key)
        if not service:
            return {"status": "error", "message": f"No reconozco el tipo de cita '{service_key}'."}

        normalized_phone = self._normalize_phone(patient_phone)
        if not patient_name or not normalized_phone:
            return {
                "status": "error",
                "message": "Necesito nombre del paciente y telefono para registrar la cita.",
            }

        backend_error = self._get_backend_error()
        if backend_error:
            return backend_error

        start_dt = self._parse_datetime(start_at)
        if not start_dt:
            return {
                "status": "error",
                "message": "Formato de fecha invalido. Usa YYYY-MM-DD HH:MM.",
            }

        end_dt = start_dt + timedelta(minutes=service.duration_minutes)
        validation_error = self._validate_slot_window(start_dt, end_dt)
        if validation_error:
            return {"status": "error", "message": validation_error}

        conflict = self._find_conflict(start_dt, end_dt)
        if conflict:
            return {
                "status": "error",
                "message": "Ese horario ya no esta disponible.",
                "conflict": self._serialize_appointment(conflict),
            }

        if self.google_calendar_ready:
            created_event = self.google_calendar.create_event(
                patient_name=patient_name.strip(),
                patient_phone=normalized_phone,
                service_key=service.key,
                service_name=service.name,
                start_dt=start_dt,
                end_dt=end_dt,
                notes=notes.strip(),
            )
            return {
                "status": "success",
                "message": "Cita creada correctamente.",
                "appointment": self._serialize_appointment(created_event),
            }

        now_iso = self._now().isoformat()
        appointment = Appointment(
            appointment_id=f"CDS-{uuid.uuid4().hex[:8].upper()}",
            patient_name=patient_name.strip(),
            patient_phone=normalized_phone,
            service_key=service.key,
            service_name=service.name,
            start_at=start_dt.isoformat(),
            end_at=end_dt.isoformat(),
            notes=notes.strip(),
            status="scheduled",
            created_at=now_iso,
            updated_at=now_iso,
        )

        appointments = self._load_appointments()
        appointments.append(appointment)
        self._save_appointments(appointments)

        return {
            "status": "success",
            "message": "Cita creada correctamente.",
            "appointment": self._serialize_appointment(appointment),
        }

    def update_appointment(
        self,
        appointment_id: str,
        start_at: Optional[str] = None,
        service_key: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Dict[str, object]:
        """Reschedule or update an existing appointment."""
        backend_error = self._get_backend_error()
        if backend_error:
            return backend_error

        if self.google_calendar_ready:
            existing_appointment = self._get_google_appointment(appointment_id)
            if not existing_appointment:
                return {"status": "error", "message": "No encontre una cita activa con ese identificador."}

            target_service = get_service(service_key or existing_appointment["service_key"])
            if not target_service:
                return {"status": "error", "message": f"No reconozco el tipo de cita '{service_key}'."}

            new_start = self._parse_datetime(start_at) if start_at else self._parse_datetime(
                existing_appointment["start_at_display"]
            )
            if start_at and not new_start:
                return {
                    "status": "error",
                    "message": "Formato de fecha invalido. Usa YYYY-MM-DD HH:MM.",
                }
            if not new_start:
                return {"status": "error", "message": "No pude interpretar la fecha actual de la cita."}

            new_end = new_start + timedelta(minutes=target_service.duration_minutes)
            validation_error = self._validate_slot_window(new_start, new_end)
            if validation_error:
                return {"status": "error", "message": validation_error}

            conflict = self._find_conflict(new_start, new_end, ignore_appointment_id=appointment_id)
            if conflict:
                return {
                    "status": "error",
                    "message": "No puedo mover la cita a ese horario porque ya esta ocupado.",
                    "conflict": self._serialize_appointment(conflict),
                }

            updated_event = self.google_calendar.update_event(
                event_id=appointment_id,
                patient_name=existing_appointment["patient_name"],
                patient_phone=existing_appointment["patient_phone"],
                service_key=target_service.key,
                service_name=target_service.name,
                start_dt=new_start,
                end_dt=new_end,
                notes=existing_appointment["notes"] if notes is None else notes.strip(),
            )
            if not updated_event:
                return {"status": "error", "message": "No encontre una cita activa con ese identificador."}

            return {
                "status": "success",
                "message": "Cita actualizada correctamente.",
                "appointment": self._serialize_appointment(updated_event),
            }

        appointments = self._load_appointments()
        appointment = next(
            (item for item in appointments if item.appointment_id == appointment_id and item.status == "scheduled"),
            None,
        )
        if not appointment:
            return {"status": "error", "message": "No encontre una cita activa con ese identificador."}

        target_service = get_service(service_key or appointment.service_key)
        if not target_service:
            return {"status": "error", "message": f"No reconozco el tipo de cita '{service_key}'."}

        new_start = self._parse_datetime(start_at) if start_at else datetime.fromisoformat(appointment.start_at)
        if start_at and not new_start:
            return {
                "status": "error",
                "message": "Formato de fecha invalido. Usa YYYY-MM-DD HH:MM.",
            }

        if new_start.tzinfo is None:
            new_start = new_start.replace(tzinfo=self.timezone)
        new_end = new_start + timedelta(minutes=target_service.duration_minutes)

        validation_error = self._validate_slot_window(new_start, new_end)
        if validation_error:
            return {"status": "error", "message": validation_error}

        conflict = self._find_conflict(new_start, new_end, ignore_appointment_id=appointment_id)
        if conflict:
            return {
                "status": "error",
                "message": "No puedo mover la cita a ese horario porque ya esta ocupado.",
                "conflict": self._serialize_appointment(conflict),
            }

        appointment.service_key = target_service.key
        appointment.service_name = target_service.name
        appointment.start_at = new_start.isoformat()
        appointment.end_at = new_end.isoformat()
        if notes is not None:
            appointment.notes = notes.strip()
        appointment.updated_at = self._now().isoformat()

        self._save_appointments(appointments)
        return {
            "status": "success",
            "message": "Cita actualizada correctamente.",
            "appointment": self._serialize_appointment(appointment),
        }

    def cancel_appointment(self, appointment_id: str, cancellation_reason: str = "") -> Dict[str, object]:
        """Cancel an appointment while keeping an audit trail."""
        backend_error = self._get_backend_error()
        if backend_error:
            return backend_error

        if self.google_calendar_ready:
            cancelled_event = self.google_calendar.cancel_event(
                event_id=appointment_id,
                cancellation_reason=cancellation_reason,
            )
            if not cancelled_event:
                return {"status": "error", "message": "No encontre una cita activa con ese identificador."}

            return {
                "status": "success",
                "message": "Cita cancelada correctamente.",
                "appointment": self._serialize_appointment(cancelled_event),
            }

        appointments = self._load_appointments()
        appointment = next(
            (item for item in appointments if item.appointment_id == appointment_id and item.status == "scheduled"),
            None,
        )
        if not appointment:
            return {"status": "error", "message": "No encontre una cita activa con ese identificador."}

        appointment.status = "cancelled"
        appointment.cancellation_reason = cancellation_reason.strip()
        appointment.updated_at = self._now().isoformat()
        self._save_appointments(appointments)

        return {
            "status": "success",
            "message": "Cita cancelada correctamente.",
            "appointment": self._serialize_appointment(appointment),
        }

    def _ensure_storage(self) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.storage_path.exists():
            self.storage_path.write_text("[]", encoding="utf-8")

    def _load_appointments(self) -> List[Appointment]:
        with _STORAGE_LOCK:
            raw_data = json.loads(self.storage_path.read_text(encoding="utf-8"))
        return [Appointment(**item) for item in raw_data]

    def _save_appointments(self, appointments: List[Appointment]) -> None:
        payload = [asdict(appointment) for appointment in appointments]
        with _STORAGE_LOCK:
            self.storage_path.write_text(
                json.dumps(payload, ensure_ascii=True, indent=2),
                encoding="utf-8",
            )

    def _find_conflict(
        self,
        start_dt: datetime,
        end_dt: datetime,
        ignore_appointment_id: Optional[str] = None,
    ):
        if self.google_calendar_ready:
            return self.google_calendar.find_conflict(start_dt, end_dt, ignore_appointment_id)

        for appointment in self._load_appointments():
            if appointment.status != "scheduled":
                continue
            if ignore_appointment_id and appointment.appointment_id == ignore_appointment_id:
                continue

            appointment_start = datetime.fromisoformat(appointment.start_at)
            appointment_end = datetime.fromisoformat(appointment.end_at)
            if start_dt < appointment_end and end_dt > appointment_start:
                return appointment
        return None

    def _validate_slot_window(self, start_dt: datetime, end_dt: datetime) -> Optional[str]:
        if start_dt < self._now():
            return "No puedo reservar citas en horarios pasados."

        weekday = start_dt.weekday()
        if weekday not in WORKING_HOURS:
            return "La clinica no atiende ese dia."

        opening_time, closing_time = WORKING_HOURS[weekday]
        opening_dt = self._combine_date_time(start_dt.date(), opening_time)
        closing_dt = self._combine_date_time(start_dt.date(), closing_time)

        if start_dt < opening_dt or end_dt > closing_dt:
            return "Ese horario queda fuera del horario laboral de la clinica."

        for break_start, break_end in BREAK_WINDOWS.get(weekday, []):
            break_start_dt = self._combine_date_time(start_dt.date(), break_start)
            break_end_dt = self._combine_date_time(start_dt.date(), break_end)
            if start_dt < break_end_dt and end_dt > break_start_dt:
                return "Ese horario cruza una pausa interna de la clinica."

        return None

    def _iter_day_slots(self, target_date: date, duration_minutes: int):
        weekday = target_date.weekday()
        opening_time, closing_time = WORKING_HOURS[weekday]
        current = self._combine_date_time(target_date, opening_time)
        end_limit = self._combine_date_time(target_date, closing_time)
        duration = timedelta(minutes=duration_minutes)

        while current + duration <= end_limit:
            candidate_end = current + duration
            if not self._validate_slot_window(current, candidate_end):
                yield current
            current += timedelta(minutes=SLOT_INTERVAL_MINUTES)

    def _serialize_appointment(self, appointment) -> Dict[str, str]:
        if isinstance(appointment, dict):
            return self._serialize_calendar_event(appointment)

        start_dt = datetime.fromisoformat(appointment.start_at)
        end_dt = datetime.fromisoformat(appointment.end_at)
        return {
            "appointment_id": appointment.appointment_id,
            "patient_name": appointment.patient_name,
            "patient_phone": appointment.patient_phone,
            "service_key": appointment.service_key,
            "service_name": appointment.service_name,
            "status": appointment.status,
            "notes": appointment.notes,
            "cancellation_reason": appointment.cancellation_reason,
            "start_at": appointment.start_at,
            "end_at": appointment.end_at,
            "start_at_display": self._format_datetime(start_dt),
            "end_at_display": self._format_datetime(end_dt),
            "add_to_calendar_link": self._build_add_to_calendar_link(appointment.appointment_id),
        }

    def _serialize_calendar_event(self, event: Dict[str, object]) -> Dict[str, str]:
        metadata = self._get_event_metadata(event)
        start_dt, end_dt = self.google_calendar.extract_event_window(event)
        start_at = start_dt.isoformat() if start_dt else ""
        end_at = end_dt.isoformat() if end_dt else ""
        return {
            "appointment_id": str(event.get("id", "")),
            "patient_name": metadata.get("patient_name", ""),
            "patient_phone": metadata.get("patient_phone", ""),
            "service_key": metadata.get("service_key", ""),
            "service_name": metadata.get("service_name") or str(event.get("summary", "")),
            "status": "cancelled" if event.get("status") == "cancelled" else "scheduled",
            "notes": metadata.get("notes", ""),
            "cancellation_reason": metadata.get("cancellation_reason", ""),
            "start_at": start_at,
            "end_at": end_at,
            "start_at_display": self._format_datetime(start_dt) if start_dt else "",
            "end_at_display": self._format_datetime(end_dt) if end_dt else "",
            "calendar_link": str(event.get("htmlLink", "")),
            "add_to_calendar_link": self._build_add_to_calendar_link(str(event.get("id", ""))),
        }

    def _format_slot(self, start_dt: datetime, end_dt: datetime) -> Dict[str, str]:
        return {
            "start_at": start_dt.isoformat(),
            "end_at": end_dt.isoformat(),
            "start_at_display": self._format_datetime(start_dt),
            "end_at_display": self._format_datetime(end_dt),
            "weekday": WEEKDAY_LABELS[start_dt.weekday()],
        }

    def _format_datetime(self, value: datetime) -> str:
        localized = value.astimezone(self.timezone)
        return localized.strftime("%Y-%m-%d %H:%M")

    def _build_add_to_calendar_link(self, appointment_id: str) -> str:
        if not appointment_id:
            return ""
        public_url = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
        if not public_url:
            return ""
        return f"{public_url}/calendar/{appointment_id}.ics"

    def _parse_datetime(self, value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None

        normalized = value.strip().replace("/", "-")
        for candidate in (
            normalized,
            normalized.replace("T", " "),
        ):
            for parser in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
                try:
                    parsed = datetime.strptime(candidate, parser)
                    return parsed.replace(tzinfo=self.timezone)
                except ValueError:
                    continue

        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            parsed = self._parse_natural_datetime(normalized)
            if not parsed:
                return None

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self.timezone)
        return parsed.astimezone(self.timezone)

    def _parse_date(self, value: Optional[str]) -> Optional[date]:
        if not value:
            return None

        normalized = self._normalize_text(value)
        detected_reference = self._extract_date_reference(normalized)
        if detected_reference:
            return detected_reference

        date_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", normalized)
        if date_match:
            try:
                return datetime.strptime(date_match.group(1), "%Y-%m-%d").date()
            except ValueError:
                return None

        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d").date()
        except ValueError:
            return None

    def _normalize_phone(self, phone: Optional[str]) -> str:
        if not phone:
            return ""
        return "".join(char for char in str(phone) if char.isdigit())

    def _combine_date_time(self, target_date: date, hhmm_value: str) -> datetime:
        hour, minute = (int(part) for part in hhmm_value.split(":"))
        return datetime.combine(target_date, time(hour=hour, minute=minute), tzinfo=self.timezone)

    def _matches_time_of_day(self, start_dt: datetime, preferred_time_of_day: str) -> bool:
        preference = (preferred_time_of_day or "any").strip().lower()
        if preference == "any":
            return True
        if preference in {"morning", "manana", "mañana"}:
            return start_dt.hour < 12
        if preference in {"afternoon", "tarde"}:
            return start_dt.hour >= 12
        return True

    def _now(self) -> datetime:
        return datetime.now(self.timezone)

    def _rounded_now(self) -> datetime:
        current = self._now()
        rounded_minute = ((current.minute + SLOT_INTERVAL_MINUTES - 1) // SLOT_INTERVAL_MINUTES) * SLOT_INTERVAL_MINUTES
        if rounded_minute >= 60:
            current = current.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        else:
            current = current.replace(minute=rounded_minute, second=0, microsecond=0)
        return current

    def _get_backend_error(self) -> Optional[Dict[str, object]]:
        if self.google_calendar_requested and not self.google_calendar_ready:
            logging.error("AppointmentManager backend configuration error: %s", self.google_calendar.get_configuration_error())
            return {
                "status": "error",
                "message": self.google_calendar.get_configuration_error(),
            }
        if self.google_calendar_ready:
            runtime_error = self.google_calendar.get_runtime_error()
            if runtime_error:
                logging.error("AppointmentManager backend runtime error: %s", runtime_error)
                return {
                    "status": "error",
                    "message": runtime_error,
                }
        return None

    def _get_event_metadata(self, event: Dict[str, object]) -> Dict[str, str]:
        if not self.google_calendar_ready:
            return {}
        return self.google_calendar.get_private_metadata(event)

    def _get_google_appointment(self, appointment_id: str) -> Optional[Dict[str, str]]:
        event = self.google_calendar.get_event(appointment_id)
        if not event or event.get("status") == "cancelled":
            return None
        return self._serialize_appointment(event)

    def _parse_natural_datetime(self, raw_value: str) -> Optional[datetime]:
        normalized = self._normalize_text(raw_value)
        target_date = self._extract_date_reference(normalized)
        target_time = self._extract_time_reference(normalized)

        if not target_date or not target_time:
            return None

        return datetime.combine(target_date, target_time, tzinfo=self.timezone)

    def _extract_date_reference(self, normalized: str) -> Optional[date]:
        if "pasado manana" in normalized or "day after tomorrow" in normalized:
            return self._now().date() + timedelta(days=2)
        if "manana" in normalized or "tomorrow" in normalized:
            return self._now().date() + timedelta(days=1)
        if "hoy" in normalized or "today" in normalized:
            return self._now().date()

        weekday_reference = self._extract_weekday_reference(normalized)
        if weekday_reference is not None:
            return self._next_weekday_date(weekday_reference)

        date_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", normalized)
        if date_match:
            try:
                return datetime.strptime(date_match.group(1), "%Y-%m-%d").date()
            except ValueError:
                return None

        return None

    def _extract_time_reference(self, normalized: str) -> Optional[time]:
        if "mediodia" in normalized or "medio dia" in normalized or "noon" in normalized:
            return time(hour=12, minute=0)
        if "medianoche" in normalized or "midnight" in normalized:
            return time(hour=0, minute=0)

        match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", normalized)
        if match:
            hour = int(match.group(1))
            minute = int(match.group(2) or 0)
            meridiem = match.group(3)
            if meridiem == "pm" and hour != 12:
                hour += 12
            if meridiem == "am" and hour == 12:
                hour = 0
            return time(hour=hour, minute=minute)

        match = re.search(r"\b(?:a las|las|a la|la)\s*(\d{1,2})(?::(\d{2}))?\b", normalized)
        if not match:
            compact = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", normalized)
            match = re.search(r"(?<![\d-])(\d{1,2})(?::(\d{2}))?(?![\d-])", compact)
            if not match:
                return None

        hour = int(match.group(1))
        minute = int(match.group(2) or 0)

        if any(token in normalized for token in ("de la tarde", "por la tarde", "afternoon")) and hour < 12:
            hour += 12
        elif any(token in normalized for token in ("de la noche", "por la noche", "night", "evening")) and hour < 12:
            hour += 12
        elif any(token in normalized for token in ("de la manana", "por la manana", "morning")):
            if hour == 12:
                hour = 0

        if hour > 23 or minute > 59:
            return None

        return time(hour=hour, minute=minute)

    def _extract_weekday_reference(self, normalized: str) -> Optional[int]:
        for weekday_name, weekday_index in WEEKDAY_NAME_TO_INDEX.items():
            if re.search(rf"\b{re.escape(weekday_name)}\b", normalized):
                return weekday_index
        return None

    def _next_weekday_date(self, weekday_index: int) -> date:
        today = self._now().date()
        days_ahead = (weekday_index - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        return today + timedelta(days=days_ahead)

    def _normalize_text(self, value: str) -> str:
        text = unicodedata.normalize("NFD", str(value).strip().lower())
        text = "".join(char for char in text if unicodedata.category(char) != "Mn")
        text = re.sub(r"[,\.;]", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text
