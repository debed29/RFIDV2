import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app_ui import RfidApp, format_name_for_display


class ReportCountTests(unittest.TestCase):
    def test_registered_student_count_is_not_student_attendance_row_count(self):
        app = SimpleNamespace(
            users=[
                {"role": "student"},
                {"role": "student"},
                {"role": "visitor"},
            ],
            logs=[{"role": "student"} for _ in range(7)],
        )

        counts = RfidApp.report_counts(app)

        self.assertEqual(counts["total"], 7)
        self.assertEqual(counts["students"], 2)
        self.assertEqual(counts["visitors"], 1)
        self.assertEqual(counts["registered_total"], 3)


class DisplayNameTests(unittest.TestCase):
    def test_surname_first_name_and_middle_name_are_displayed_in_order(self):
        record = {
            "name": "First Middle Surname",
            "surname": "Surname",
            "first_name": "First",
            "middle_name": "Middle",
        }

        self.assertEqual(format_name_for_display(record), "Surname First Middle")

    def test_missing_name_parts_fall_back_to_existing_name(self):
        self.assertEqual(
            format_name_for_display({"name": "Existing Display Name"}),
            "Existing Display Name",
        )


if __name__ == "__main__":
    unittest.main()
