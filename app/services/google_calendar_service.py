"""Google Calendar integration for appointment management."""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from app.services.clinic_profile import CLINIC_NAME, CLINIC_TIMEZONE


class GoogleCalendarService:
    """Thin wrapper around Google Calendar API using a service account."""

    SCOPES = ["https://www.googleapis.com/auth/calendar"]

    def __init__(
        self,
        service_account_file: Optional[str] = None,
        calendar_id: Optional[str] = None,
        timezone: str = CLINIC_TIMEZONE,
    ):
        load_dotenv()
        configured_file = service_account_file or os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "")
        self.service_account_file = self._resolve_service_account_file(configured_file)
        self.calendar_id = (calendar_id or os.getenv("GOOGLE_CALENDAR_ID", "")).strip()
        self.timezone = timezone
        self._client = None

    def is_requested(self) -> bool:
        """Return whether Google Calendar integration is expected to be active."""
        return bool(self.calendar_id or self.service_account_file)

    def get_configuration_error(self) -> str:
        """Return a human-readable configuration error, if any."""
        if not self.calendar_id:
            return "Falta GOOGLE_CALENDAR_ID en el archivo .env."
        if not self.service_account_file:
            return "Falta GOOGLE_SERVICE_ACCOUNT_FILE en el archivo .env."
        if not self.service_account_file.exists():
            return (
                "No encuentro el archivo JSON de la cuenta de servicio en "
                f"{self.service_account_file}."
            )
        return ""

    def get_runtime_error(self) -> str:
        """Return any runtime issue that would block Calendar API usage."""
        configuration_error = self.get_configuration_error()
        if configuration_error:
            return configuration_error
        try:
            from google.oauth2 import service_account  # noqa: F401
            from googleapiclient.discovery import build  # noqa: F401
        except ImportError:
            executable = Path(sys.executable).expanduser()
            local_python = Path(".venv/bin/python").resolve()
            if executable.resolve() != local_python:
                return (
                    "Faltan dependencias de Google Calendar en el Python actual "
                    f"({executable}). Inicia el bot con .venv/bin/python run.py "
                    "o instala requirements.txt en ese mismo interprete."
                )
            return (
                "Faltan dependencias de Google Calendar en el Python actual "
                f"({executable}). Ejecuta {executable} -m pip install -r requirements.txt."
            )
        return ""

    def list_events(
        self,
        time_min: Optional[datetime] = None,
        time_max: Optional[datetime] = None,
        max_results: int = 250,
    ) -> List[Dict[str, object]]:
        """List calendar events within the requested window."""
        client = self._get_client()
        logging.info(
            "Google Calendar list_events calendar_id=%s time_min=%s time_max=%s max_results=%s",
            self.calendar_id,
            self._to_rfc3339(time_min) if time_min else None,
            self._to_rfc3339(time_max) if time_max else None,
            max_results,
        )
        request_kwargs = {
            "calendarId": self.calendar_id,
            "singleEvents": True,
            "orderBy": "startTime",
            "maxResults": max_results,
        }
        if time_min:
            request_kwargs["timeMin"] = self._to_rfc3339(time_min)
        if time_max:
            request_kwargs["timeMax"] = self._to_rfc3339(time_max)

        events: List[Dict[str, object]] = []
        page_token = None
        while True:
            if page_token:
                request_kwargs["pageToken"] = page_token
            response = client.events().list(**request_kwargs).execute()
            events.extend(response.get("items", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        logging.info("Google Calendar list_events returned count=%s", len(events))
        return events

    def get_event(self, event_id: str) -> Optional[Dict[str, object]]:
        """Fetch a single event by its Google Calendar identifier."""
        client = self._get_client()
        logging.info("Google Calendar get_event event_id=%s", event_id)
        try:
            return client.events().get(
                calendarId=self.calendar_id,
                eventId=event_id,
            ).execute()
        except Exception as exc:
            if getattr(getattr(exc, "resp", None), "status", None) == 404:
                return None
            raise

    def find_conflict(
        self,
        start_dt: datetime,
        end_dt: datetime,
        ignore_event_id: Optional[str] = None,
    ) -> Optional[Dict[str, object]]:
        """Find the first active event that overlaps the requested slot."""
        events = self.list_events(time_min=start_dt, time_max=end_dt, max_results=50)
        for event in events:
            if event.get("status") == "cancelled":
                continue
            if ignore_event_id and event.get("id") == ignore_event_id:
                continue

            event_start, event_end = self.extract_event_window(event)
            if not event_start or not event_end:
                continue
            if start_dt < event_end and end_dt > event_start:
                return event
        return None

    def find_patient_events(
        self,
        patient_phone: str,
        patient_name: Optional[str] = None,
    ) -> List[Dict[str, object]]:
        """Return future active appointments for a given patient."""
        time_min = datetime.now(ZoneInfo(self.timezone)).replace(hour=0, minute=0, second=0, microsecond=0)
        events = self.list_events(time_min=time_min, max_results=250)
        normalized_name = (patient_name or "").strip().lower()
        matches: List[Dict[str, object]] = []

        for event in events:
            if event.get("status") == "cancelled":
                continue

            metadata = self.get_private_metadata(event)
            event_phone = (metadata.get("patient_phone") or "").strip()
            event_name = (metadata.get("patient_name") or "").strip().lower()

            if event_phone and event_phone == patient_phone:
                matches.append(event)
                continue

            if normalized_name and event_name and event_name == normalized_name:
                matches.append(event)

        matches.sort(key=lambda item: self._event_sort_key(item))
        return matches

    def find_patient_events_before(
        self,
        patient_phone: str,
        before_dt: datetime,
        patient_name: Optional[str] = None,
    ) -> List[Dict[str, object]]:
        """Return non-cancelled patient appointments scheduled before a target datetime."""
        events = self.list_events(time_max=before_dt, max_results=250)
        normalized_name = (patient_name or "").strip().lower()
        matches: List[Dict[str, object]] = []

        for event in events:
            if event.get("status") == "cancelled":
                continue

            metadata = self.get_private_metadata(event)
            event_phone = (metadata.get("patient_phone") or "").strip()
            event_name = (metadata.get("patient_name") or "").strip().lower()
            event_start, _ = self.extract_event_window(event)
            if not event_start or event_start >= before_dt:
                continue

            if event_phone and event_phone == patient_phone:
                matches.append(event)
                continue

            if normalized_name and event_name and event_name == normalized_name:
                matches.append(event)

        matches.sort(key=lambda item: self._event_sort_key(item))
        return matches

    def create_event(
        self,
        patient_name: str,
        patient_phone: str,
        service_key: str,
        service_name: str,
        start_dt: datetime,
        end_dt: datetime,
        notes: str = "",
    ) -> Dict[str, object]:
        """Create a Google Calendar event for a dental appointment."""
        client = self._get_client()
        logging.info(
            "Google Calendar create_event patient=%s service_key=%s start=%s end=%s",
            patient_name,
            service_key,
            start_dt.isoformat(),
            end_dt.isoformat(),
        )
        body = self._build_event_body(
            patient_name=patient_name,
            patient_phone=patient_phone,
            service_key=service_key,
            service_name=service_name,
            start_dt=start_dt,
            end_dt=end_dt,
            notes=notes,
        )
        return client.events().insert(
            calendarId=self.calendar_id,
            body=body,
            sendUpdates=os.getenv("GOOGLE_CALENDAR_SEND_UPDATES", "none"),
        ).execute()

    def update_event(
        self,
        event_id: str,
        patient_name: str,
        patient_phone: str,
        service_key: str,
        service_name: str,
        start_dt: datetime,
        end_dt: datetime,
        notes: str = "",
    ) -> Optional[Dict[str, object]]:
        """Patch an existing Google Calendar event."""
        client = self._get_client()
        existing_event = self.get_event(event_id)
        if not existing_event:
            return None
        logging.info(
            "Google Calendar update_event event_id=%s service_key=%s start=%s end=%s",
            event_id,
            service_key,
            start_dt.isoformat(),
            end_dt.isoformat(),
        )

        body = self._build_event_body(
            patient_name=patient_name,
            patient_phone=patient_phone,
            service_key=service_key,
            service_name=service_name,
            start_dt=start_dt,
            end_dt=end_dt,
            notes=notes,
        )
        return client.events().patch(
            calendarId=self.calendar_id,
            eventId=event_id,
            body=body,
            sendUpdates=os.getenv("GOOGLE_CALENDAR_SEND_UPDATES", "none"),
        ).execute()

    def cancel_event(self, event_id: str, cancellation_reason: str = "") -> Optional[Dict[str, object]]:
        """Cancel a Google Calendar event while preserving cancellation metadata."""
        client = self._get_client()
        existing_event = self.get_event(event_id)
        if not existing_event:
            return None
        logging.info("Google Calendar cancel_event event_id=%s", event_id)

        metadata = self.get_private_metadata(existing_event)
        metadata["cancellation_reason"] = cancellation_reason.strip()
        description = existing_event.get("description", "")
        if cancellation_reason:
            description = (
                f"{description}\nMotivo de cancelacion: {cancellation_reason.strip()}".strip()
            )

        body = {
            "status": "cancelled",
            "description": description,
            "extendedProperties": {"private": metadata},
        }
        return client.events().patch(
            calendarId=self.calendar_id,
            eventId=event_id,
            body=body,
            sendUpdates=os.getenv("GOOGLE_CALENDAR_SEND_UPDATES", "none"),
        ).execute()

    def extract_event_window(self, event: Dict[str, object]):
        """Return localized start/end datetimes for a Google Calendar event."""
        start_dt = self._parse_event_datetime(event.get("start", {}))
        end_dt = self._parse_event_datetime(event.get("end", {}))
        return start_dt, end_dt

    def get_private_metadata(self, event: Dict[str, object]) -> Dict[str, str]:
        """Expose private extended properties in a normalized structure."""
        return self._get_private_metadata(event)

    def _get_client(self):
        error = self.get_runtime_error()
        if error:
            raise RuntimeError(error)

        if self._client is not None:
            return self._client

        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise RuntimeError(
                "Faltan dependencias de Google Calendar. Instala requirements.txt nuevamente."
            ) from exc

        credentials = service_account.Credentials.from_service_account_file(
            str(self.service_account_file),
            scopes=self.SCOPES,
        )
        self._client = build("calendar", "v3", credentials=credentials, cache_discovery=False)
        logging.info(
            "Google Calendar client initialized calendar_id=%s service_account_file=%s",
            self.calendar_id,
            self.service_account_file,
        )
        return self._client

    def _build_event_body(
        self,
        patient_name: str,
        patient_phone: str,
        service_key: str,
        service_name: str,
        start_dt: datetime,
        end_dt: datetime,
        notes: str,
    ) -> Dict[str, object]:
        metadata = {
            "appointment_source": "whatsapp-bot",
            "clinic_name": CLINIC_NAME,
            "patient_name": patient_name.strip(),
            "patient_phone": patient_phone.strip(),
            "service_key": service_key.strip(),
            "service_name": service_name.strip(),
            "notes": notes.strip(),
        }
        description_lines = [
            f"Clinica: {CLINIC_NAME}",
            f"Paciente: {patient_name.strip()}",
            f"Telefono: {patient_phone.strip()}",
            f"Servicio: {service_name.strip()}",
            f"Clave de servicio: {service_key.strip()}",
        ]
        if notes.strip():
            description_lines.append(f"Notas: {notes.strip()}")

        return {
            "summary": f"{service_name.strip()} - {patient_name.strip()}",
            "location": CLINIC_NAME,
            "description": "\n".join(description_lines),
            "start": {
                "dateTime": start_dt.astimezone(ZoneInfo(self.timezone)).isoformat(),
                "timeZone": self.timezone,
            },
            "end": {
                "dateTime": end_dt.astimezone(ZoneInfo(self.timezone)).isoformat(),
                "timeZone": self.timezone,
            },
            "extendedProperties": {"private": metadata},
        }

    def _get_private_metadata(self, event: Dict[str, object]) -> Dict[str, str]:
        extended = event.get("extendedProperties", {}) or {}
        private = extended.get("private", {}) or {}
        return {str(key): str(value) for key, value in private.items()}

    def _parse_event_datetime(self, payload: Dict[str, object]) -> Optional[datetime]:
        if not payload:
            return None

        datetime_value = payload.get("dateTime")
        if datetime_value:
            return datetime.fromisoformat(datetime_value.replace("Z", "+00:00")).astimezone(
                ZoneInfo(self.timezone)
            )

        date_value = payload.get("date")
        if date_value:
            return datetime.fromisoformat(date_value).replace(tzinfo=ZoneInfo(self.timezone))

        return None

    def _event_sort_key(self, event: Dict[str, object]) -> str:
        start_payload = event.get("start", {}) or {}
        return str(start_payload.get("dateTime") or start_payload.get("date") or "")

    def _to_rfc3339(self, value: datetime) -> str:
        return value.astimezone(ZoneInfo(self.timezone)).isoformat()

    def _resolve_service_account_file(self, configured_file: str) -> Optional[Path]:
        if configured_file:
            candidate = Path(configured_file).expanduser()
            if candidate.exists():
                return candidate

        secrets_dir = Path("secrets")
        json_files = sorted(secrets_dir.glob("*.json"))
        if len(json_files) == 1:
            return json_files[0]

        if configured_file:
            return Path(configured_file).expanduser()
        return None
