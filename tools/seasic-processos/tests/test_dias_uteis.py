from __future__ import annotations

from datetime import date
from pathlib import Path
import tempfile
import unittest

from seasic_monitor.database import MonitorDatabase
from seasic_monitor.domain import (
    CollectionStatus,
    Observation,
    ProcessRecord,
    StagnationRule,
    brazil_national_holidays,
    easter,
)
from seasic_monitor.reporting import render_current_view


BUSINESS = StagnationRule(limit=3, business_days=True)


class BusinessDayTests(unittest.TestCase):
    def test_weekend_and_national_holiday_are_skipped(self) -> None:
        # Sexta 09/10/2026 → terça 13/10/2026; segunda 12/10 é feriado nacional.
        self.assertEqual(BUSINESS.days_between(date(2026, 10, 9), date(2026, 10, 13)), 1)
        self.assertEqual(StagnationRule().days_between(date(2026, 10, 9), date(2026, 10, 13)), 4)

    def test_good_friday_follows_easter(self) -> None:
        self.assertEqual(easter(2026), date(2026, 4, 5))
        self.assertIn(date(2026, 4, 3), brazil_national_holidays(2026))
        self.assertEqual(easter(2027), date(2027, 3, 28))

    def test_configured_holidays_and_kind(self) -> None:
        rule = StagnationRule.from_config(
            {"stagnant_after_days": 5, "holidays": ["2026-07-08"]}
        )
        self.assertTrue(rule.business_days)  # padrão: dias úteis
        self.assertFalse(rule.is_business_day(date(2026, 7, 8)))  # quarta, feriado de SE
        self.assertTrue(rule.is_business_day(date(2026, 7, 9)))
        self.assertFalse(StagnationRule.from_config({"stagnant_day_kind": "corridos"}).business_days)
        with self.assertRaises(ValueError):
            StagnationRule.from_config({"holidays": ["08/07/2026"]})
        with self.assertRaises(ValueError):
            StagnationRule.from_config({"stagnant_day_kind": "semanas"})

    def test_same_or_earlier_day_counts_zero(self) -> None:
        self.assertEqual(BUSINESS.days_between(date(2026, 10, 6), date(2026, 10, 6)), 0)
        self.assertEqual(BUSINESS.days_between(date(2026, 10, 7), date(2026, 10, 6)), 0)


class BusinessDayStagnationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = MonitorDatabase(Path(self.temp_dir.name) / "monitor.sqlite")
        self.database.initialize()
        self.database.upsert_process(ProcessRecord(system="SEI", number="1/2026"))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def record(self, movement_date: str, collected_at: str, rule) -> dict:
        run_id = self.database.create_execution(mode="TESTE")
        return self.database.record_observation(
            run_id,
            Observation(
                system="SEI",
                number="1/2026",
                collected_at=collected_at,
                status=CollectionStatus.OK,
                units=("GAB",),
                last_movement="Recebido",
                movement_date=movement_date,
            ),
            rule,
        )

    def test_stagnation_uses_business_days(self) -> None:
        # 09/10 (sex) → 14/10 (qua): 6 dias corridos, 2 dias úteis.
        result = self.record("2026-10-09", "2026-10-14T12:00:00+00:00", BUSINESS)
        self.assertEqual(result["occurrences_added"], 0)
        result = self.record("2026-10-09", "2026-10-15T12:00:00+00:00", BUSINESS)
        self.assertEqual(result["occurrences_added"], 1)
        pending = self.database.pending_occurrences()["occurrences"]
        self.assertIn("3 dias úteis", pending[0]["description"])

    def test_current_view_names_the_business_day_column(self) -> None:
        self.record("2026-10-09", "2026-10-13T12:00:00+00:00", BUSINESS)
        text = render_current_view(
            self.database.current_view(), "csv", today=date(2026, 10, 13), rule=BUSINESS
        )
        header, row = text.splitlines()[:2]
        self.assertIn("Dias úteis sem movimento", header)
        self.assertEqual(row.split(",")[6], "1")


if __name__ == "__main__":
    unittest.main()
