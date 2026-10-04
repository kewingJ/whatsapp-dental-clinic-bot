"""Static clinic data used by the assistant and the scheduling layer."""

from __future__ import annotations

import os
from datetime import date
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Tuple


CLINIC_NAME = "Demo Dental Clinic"
CLINIC_ADDRESS = "123 Main Street, Your City"
CLINIC_TIMEZONE = os.getenv("CLINIC_TIMEZONE", "America/Managua")
SLOT_INTERVAL_MINUTES = 30
DOCTOR_NAME = "Dr. Demo Dentist"
DOCTOR_REFERENCE = DOCTOR_NAME
FIRST_VISIT_DISCOUNT_PERCENT = 20
FIRST_VISIT_DISCOUNT_MONTHS = (3, 4)

WORKING_HOURS: Dict[int, Tuple[str, str]] = {
    0: ("08:00", "17:00"),
    1: ("08:00", "17:00"),
    2: ("08:00", "17:00"),
    3: ("08:00", "17:00"),
    4: ("08:00", "17:00"),
    5: ("08:00", "13:00"),
}

BREAK_WINDOWS: Dict[int, List[Tuple[str, str]]] = {
    0: [("12:30", "13:30")],
    1: [("12:30", "13:30")],
    2: [("12:30", "13:30")],
    3: [("12:30", "13:30")],
    4: [("12:30", "13:30")],
    5: [],
}

WEEKDAY_LABELS = {
    0: "Lunes",
    1: "Martes",
    2: "Miercoles",
    3: "Jueves",
    4: "Viernes",
    5: "Sabado",
    6: "Domingo",
}


@dataclass(frozen=True)
class DentalService:
    """Represents a bookable appointment type."""

    key: str
    name: str
    price_usd: float
    duration_minutes: int
    category: str
    description: str
    booking_note: str = ""
    requires_evaluation: bool = False


_SERVICES: Tuple[DentalService, ...] = (
    DentalService(
        key="consulta_valoracion",
        name="Consulta de valoracion",
        price_usd=25.0,
        duration_minutes=45,
        category="diagnostico",
        description="Primera visita, chequeo general, dolor dental inicial o revision preventiva.",
        booking_note="Ideal cuando el paciente aun no sabe exactamente que tratamiento necesita.",
    ),
    DentalService(
        key="limpieza_rutina",
        name="Limpieza dental rutinaria",
        price_usd=35.0,
        duration_minutes=60,
        category="prevencion",
        description="Profilaxis, revision preventiva y recomendaciones de higiene.",
    ),
    DentalService(
        key="limpieza_profunda",
        name="Limpieza profunda",
        price_usd=80.0,
        duration_minutes=90,
        category="periodoncia",
        description="Escalado y alisado radicular para pacientes con acumulacion importante o enfermedad periodontal.",
        booking_note="Puede requerir anestesia local y, segun el caso, dividirse en mas de una cita.",
    ),
    DentalService(
        key="resina_dental",
        name="Resina u obturacion dental",
        price_usd=45.0,
        duration_minutes=60,
        category="restauracion",
        description="Tratamiento de caries leves a moderadas y reparacion de pequenas fracturas.",
    ),
    DentalService(
        key="extraccion_simple",
        name="Extraccion simple",
        price_usd=60.0,
        duration_minutes=40,
        category="cirugia",
        description="Extraccion de piezas dentales de baja complejidad.",
        booking_note="La complejidad final se confirma en consulta.",
        requires_evaluation=True,
    ),
    DentalService(
        key="endodoncia",
        name="Endodoncia",
        price_usd=140.0,
        duration_minutes=90,
        category="especialidad",
        description="Tratamiento de conducto para piezas con infeccion pulpar o dolor persistente.",
        booking_note="El diagnostico definitivo siempre lo confirma el dentista tras la valoracion.",
        requires_evaluation=True,
    ),
    DentalService(
        key="corona_dental",
        name="Corona dental",
        price_usd=180.0,
        duration_minutes=60,
        category="rehabilitacion",
        description="Preparacion o control de una corona para restaurar una pieza debilitada.",
        booking_note="Normalmente requiere plan de tratamiento y mas de una visita.",
        requires_evaluation=True,
    ),
    DentalService(
        key="blanqueamiento",
        name="Blanqueamiento dental",
        price_usd=120.0,
        duration_minutes=90,
        category="estetica",
        description="Sesion de blanqueamiento dental en clinica.",
        booking_note="Antes de confirmar conviene verificar que el paciente no tenga sensibilidad severa ni caries activas.",
    ),
    DentalService(
        key="urgencia_dental",
        name="Urgencia dental",
        price_usd=30.0,
        duration_minutes=30,
        category="urgencias",
        description="Atencion prioritaria para dolor fuerte, inflamacion, trauma o restauracion desprendida.",
    ),
    DentalService(
        key="control_post_tratamiento",
        name="Control post tratamiento",
        price_usd=20.0,
        duration_minutes=30,
        category="seguimiento",
        description="Revision posterior a extraccion, endodoncia, limpieza profunda u otro tratamiento.",
    ),
)

SERVICES_BY_KEY = {service.key: service for service in _SERVICES}


def get_service(key: Optional[str]) -> Optional[DentalService]:
    """Return a service by its public key."""
    if not key:
        return None
    return SERVICES_BY_KEY.get(key.strip().lower())


def get_service_catalog() -> List[DentalService]:
    """Return all bookable services."""
    return list(_SERVICES)


def serialize_service(service: DentalService) -> Dict[str, object]:
    """Convert a service into a JSON-friendly structure."""
    return asdict(service)


def get_business_hours_summary() -> str:
    """Human-readable schedule summary for prompts and docs."""
    lines: List[str] = []
    for weekday in range(6):
        start_at, end_at = WORKING_HOURS[weekday]
        breaks = BREAK_WINDOWS.get(weekday, [])
        break_text = ""
        if breaks:
            joined_breaks = ", ".join(f"{start} a {end}" for start, end in breaks)
            break_text = f" | pausas: {joined_breaks}"
        lines.append(f"- {WEEKDAY_LABELS[weekday]}: {start_at} a {end_at}{break_text}")
    lines.append("- Domingo: cerrado")
    return "\n".join(lines)


def get_services_summary() -> str:
    """Human-readable service summary for the assistant prompt."""
    lines: List[str] = []
    for service in get_service_catalog():
        note = f" Nota: {service.booking_note}" if service.booking_note else ""
        lines.append(
            f"- {service.name} ({service.key}) | US${service.price_usd:.2f} | {service.duration_minutes} min | "
            f"{service.description}{note}"
        )
    return "\n".join(lines)


def is_first_visit_discount_eligible(appointment_date: Optional[date]) -> bool:
    """Return whether the first-visit seasonal discount applies to this date."""
    if not appointment_date:
        return False
    return appointment_date.month in FIRST_VISIT_DISCOUNT_MONTHS


def calculate_service_price(service: DentalService, appointment_date: Optional[date], is_first_visit: bool) -> Dict[str, float]:
    """Calculate base and final price, applying the seasonal first-visit discount when relevant."""
    base_price = float(service.price_usd)
    discount_percent = float(FIRST_VISIT_DISCOUNT_PERCENT if is_first_visit and is_first_visit_discount_eligible(appointment_date) else 0)
    discount_amount = round(base_price * (discount_percent / 100), 2)
    final_price = round(base_price - discount_amount, 2)
    return {
        "base_price_usd": round(base_price, 2),
        "discount_percent": discount_percent,
        "discount_amount_usd": discount_amount,
        "final_price_usd": final_price,
    }
