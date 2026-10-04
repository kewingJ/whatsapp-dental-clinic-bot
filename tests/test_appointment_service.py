"""Unit tests for AppointmentManager business logic and validation."""

import os
import tempfile
import unittest
from datetime import timedelta

from app.services.appointment_service import AppointmentManager


class TestAppointmentService(unittest.TestCase):
    """Test suite verifying business rules, conflict detection and scheduling logic."""

    def setUp(self):
        # Create an isolated temporary file for JSON appointments storage
        self.temp_file = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.temp_file.write(b"[]")
        self.temp_file.close()

        # Instantiate manager with the temporary file and explicitly disable Google Calendar
        self.manager = AppointmentManager(storage_path=self.temp_file.name)
        self.manager.google_calendar_requested = False
        self.manager.google_calendar_ready = False

    def tearDown(self):
        if os.path.exists(self.temp_file.name):
            os.remove(self.temp_file.name)

    def _get_future_weekday(self, target_weekday: int = 2, weeks_ahead: int = 2):
        """Helper to find a future date matching target_weekday (0=Mon, 2=Wed, 6=Sun)."""
        base = self.manager._now().date() + timedelta(days=weeks_ahead * 7)
        while base.weekday() != target_weekday:
            base += timedelta(days=1)
        return base

    def test_check_availability_valid_slot(self):
        """A weekday morning slot within business hours should be available."""
        future_wednesday = self._get_future_weekday(target_weekday=2)  # Wednesday
        slot_str = f"{future_wednesday.isoformat()} 10:00"

        result = self.manager.check_availability("limpieza_rutina", slot_str)
        self.assertEqual(result["status"], "available")
        self.assertTrue(result["available"])
        self.assertIn("disponible", result["message"].lower())

    def test_outside_hours_early_and_late(self):
        """Slots before opening (08:00) or after closing (17:00) must be rejected."""
        future_wednesday = self._get_future_weekday(target_weekday=2)

        # Before opening (07:00)
        early_slot = f"{future_wednesday.isoformat()} 07:00"
        result_early = self.manager.check_availability("consulta_valoracion", early_slot)
        self.assertEqual(result_early["status"], "unavailable")
        self.assertFalse(result_early["available"])
        self.assertIn("fuera del horario laboral", result_early["message"])

        # After closing (17:30)
        late_slot = f"{future_wednesday.isoformat()} 17:30"
        result_late = self.manager.check_availability("consulta_valoracion", late_slot)
        self.assertEqual(result_late["status"], "unavailable")
        self.assertFalse(result_late["available"])
        self.assertIn("fuera del horario laboral", result_late["message"])

    def test_outside_hours_sunday(self):
        """Sunday is closed; appointments must be rejected."""
        future_sunday = self._get_future_weekday(target_weekday=6)  # Sunday
        sunday_slot = f"{future_sunday.isoformat()} 10:00"

        result = self.manager.check_availability("consulta_valoracion", sunday_slot)
        self.assertEqual(result["status"], "unavailable")
        self.assertFalse(result["available"])
        self.assertIn("no atiende ese dia", result["message"])

    def test_lunch_break_window_conflict(self):
        """Slots crossing or falling within lunch break (12:30 - 13:30) must be rejected."""
        future_wednesday = self._get_future_weekday(target_weekday=2)

        # Slot starting right in the lunch break (12:30)
        lunch_slot = f"{future_wednesday.isoformat()} 12:30"
        result_lunch = self.manager.check_availability("urgencia_dental", lunch_slot)
        self.assertEqual(result_lunch["status"], "unavailable")
        self.assertFalse(result_lunch["available"])
        self.assertIn("pausa interna", result_lunch["message"])

        # 60-min service starting at 12:00 would end at 13:00, crossing into lunch break
        overlapping_service_slot = f"{future_wednesday.isoformat()} 12:00"
        result_cross = self.manager.check_availability("limpieza_rutina", overlapping_service_slot)
        self.assertEqual(result_cross["status"], "unavailable")
        self.assertFalse(result_cross["available"])
        self.assertIn("pausa interna", result_cross["message"])

    def test_create_appointment_success(self):
        """Creating an appointment with valid data should succeed and persist."""
        future_wednesday = self._get_future_weekday(target_weekday=2)
        slot_str = f"{future_wednesday.isoformat()} 09:00"

        result = self.manager.create_appointment(
            patient_name="Carlos Gomez",
            patient_phone="+50588880001",
            service_key="consulta_valoracion",
            start_at=slot_str,
            notes="Primer chequeo anual",
        )

        self.assertEqual(result["status"], "success")
        self.assertIn("appointment", result)
        created = result["appointment"]
        self.assertEqual(created["patient_name"], "Carlos Gomez")
        self.assertEqual(created["status"], "scheduled")
        self.assertTrue(created["appointment_id"])

    def test_detect_overlapping_appointment_conflict(self):
        """A new booking that overlaps with an existing appointment must be rejected."""
        future_wednesday = self._get_future_weekday(target_weekday=2)
        slot_str = f"{future_wednesday.isoformat()} 10:00"

        # Book first appointment (10:00 - 11:00, 60 minutes)
        book_res = self.manager.create_appointment(
            patient_name="Ana Martinez",
            patient_phone="+50588880002",
            service_key="limpieza_rutina",
            start_at=slot_str,
        )
        self.assertEqual(book_res["status"], "success")

        # Attempt to book another appointment at exact same time
        conflict_exact = self.manager.create_appointment(
            patient_name="Pedro Ramirez",
            patient_phone="+50588880003",
            service_key="consulta_valoracion",
            start_at=slot_str,
        )
        self.assertEqual(conflict_exact["status"], "error")
        self.assertIn("ya no esta disponible", conflict_exact["message"])

        # Attempt to book overlapping slot (10:30, within the 10:00-11:00 window)
        overlap_slot = f"{future_wednesday.isoformat()} 10:30"
        conflict_overlap = self.manager.create_appointment(
            patient_name="Laura Lopez",
            patient_phone="+50588880004",
            service_key="consulta_valoracion",
            start_at=overlap_slot,
        )
        self.assertEqual(conflict_overlap["status"], "error")
        self.assertIn("ya no esta disponible", conflict_overlap["message"])

    def test_reschedule_appointment(self):
        """Rescheduling should update start_at and free the previous slot."""
        future_wednesday = self._get_future_weekday(target_weekday=2)
        orig_slot = f"{future_wednesday.isoformat()} 14:00"
        new_slot = f"{future_wednesday.isoformat()} 15:00"

        book_res = self.manager.create_appointment(
            patient_name="Sofia Chen",
            patient_phone="+50588880005",
            service_key="consulta_valoracion",
            start_at=orig_slot,
        )
        appointment_id = book_res["appointment"]["appointment_id"]

        # Reschedule to 15:00 using update_appointment
        resched_res = self.manager.update_appointment(
            appointment_id=appointment_id,
            start_at=new_slot,
        )
        self.assertEqual(resched_res["status"], "success")

        # Original slot (14:00) should now be available again
        check_orig = self.manager.check_availability("consulta_valoracion", orig_slot)
        self.assertEqual(check_orig["status"], "available")
        self.assertTrue(check_orig["available"])

    def test_cancel_appointment_releases_slot(self):
        """Cancelling an appointment marks it cancelled and frees the time slot."""
        future_wednesday = self._get_future_weekday(target_weekday=2)
        slot_str = f"{future_wednesday.isoformat()} 11:00"

        book_res = self.manager.create_appointment(
            patient_name="Luis Morales",
            patient_phone="+50588880006",
            service_key="limpieza_rutina",
            start_at=slot_str,
        )
        appointment_id = book_res["appointment"]["appointment_id"]

        # Cancel the appointment
        cancel_res = self.manager.cancel_appointment(
            appointment_id=appointment_id,
            cancellation_reason="Viaje de trabajo",
        )
        self.assertEqual(cancel_res["status"], "success")
        self.assertEqual(cancel_res["appointment"]["status"], "cancelled")

        # The slot is now free for new bookings
        check_res = self.manager.check_availability("limpieza_rutina", slot_str)
        self.assertEqual(check_res["status"], "available")
        self.assertTrue(check_res["available"])

    def test_suggest_available_slots(self):
        """Slot suggestion returns valid non-conflicting time slots for a given date."""
        future_wednesday = self._get_future_weekday(target_weekday=2)
        date_str = future_wednesday.isoformat()

        res = self.manager.suggest_available_slots(
            service_key="limpieza_rutina",
            preferred_date=date_str,
            limit=3,
        )
        self.assertEqual(res["status"], "success")
        suggestions = res.get("suggestions", [])
        self.assertGreaterEqual(len(suggestions), 1)

        # Ensure no suggested slot falls in the lunch break (12:30 - 13:30)
        for slot in suggestions:
            self.assertNotIn("12:30", slot["start_at_display"])
            self.assertNotIn("13:00", slot["start_at_display"])


if __name__ == "__main__":
    unittest.main()
