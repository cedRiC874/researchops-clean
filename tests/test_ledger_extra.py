from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from researchops_clean.ledger import (
    COMPLETION_TRANSPORT_SEND_EVENT,
    AuditError,
    AuditLedger,
    sha256_json,
    verify_audit_chain_rows,
)


class LedgerWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "ledger.sqlite3"
        self.now = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)
        self.ledger = AuditLedger(self.database, clock=lambda: self.now)

    def create_call(self, run_id: str, safe_args: dict) -> str:
        call_id = "CALL-OFFLINE"
        self.ledger.create_tool_call(
            call_id=call_id,
            run_id=run_id,
            tool_name="offline_summary",
            tool_version="1",
            policy_version="1",
            risk_class="local",
            safe_args=safe_args,
            args_hash=sha256_json(safe_args),
            approval_scope_hash=sha256_json({"scope": "offline_summary"}),
            requires_approval=True,
        )
        return call_id

    def approve(self, call_id: str) -> None:
        self.ledger.record_approval(
            call_id,
            approval_scope_hash=sha256_json({"scope": "offline_summary"}),
            decision="approve",
            approver="offline-reviewer",
            reason="Approved local summary",
            expires_at_utc=None,
        )

    def test_approval_attempt_success_workflow(self) -> None:
        run_id = self.ledger.start_run(
            mode="offline", request_summary={"objective": "summary"}
        )
        call_id = self.create_call(run_id, {"aggregate_count": 3})
        self.assertEqual(self.ledger.get_tool_call(call_id)["status"], "awaiting_approval")
        with self.assertRaises(AuditError) as caught:
            self.ledger.start_attempt(call_id)
        self.assertEqual(caught.exception.code, "tool_call_state_conflict")

        self.approve(call_id)
        self.assertEqual(self.ledger.get_tool_call(call_id)["status"], "approved")
        approval = self.ledger.get_approval(call_id)
        self.assertEqual(approval["decision"], "approve")
        self.assertEqual(
            approval["approver_subject_hash"],
            hashlib.sha256(b"offline-reviewer").hexdigest(),
        )

        attempt_no = self.ledger.start_attempt(call_id)
        self.assertEqual(attempt_no, 1)
        self.assertEqual(self.ledger.get_tool_call(call_id)["status"], "running")
        result = {"aggregate_count": 3}
        self.ledger.finish_attempt_success(
            call_id,
            attempt_no,
            duration_ms=12.5,
            safe_result=result,
            result_hash=sha256_json(result),
        )
        self.ledger.set_run_status(run_id, "completed")

        exported = self.ledger.export_run(run_id)
        self.assertEqual(exported["run"]["status"], "completed")
        self.assertEqual(exported["tool_calls"][0]["status"], "succeeded")
        self.assertEqual(exported["tool_calls"][0]["safe_result"], result)
        self.assertEqual(exported["tool_attempts"][0]["outcome"], "succeeded")
        self.assertEqual(exported["tool_attempts"][0]["duration_ms"], 12.5)
        self.assertEqual(exported["tool_attempts"][0]["attempt_no"], 1)
        self.assertEqual(
            [event["event_type"] for event in exported["events"]],
            [
                "run_started",
                "tool_call_proposed",
                "tool_approval_requested",
                "tool_approval_decided",
                "tool_attempt_started",
                "tool_attempt_succeeded",
                "run_status_changed",
            ],
        )
        self.assertTrue(exported["chain_verification"]["valid"])
        self.assertEqual(exported["chain_verification"]["event_count"], 7)

    def test_timestamp_tampering_invalidates_detached_rows(self) -> None:
        run_id = self.ledger.start_run(mode="offline", request_summary={"safe": True})
        self.ledger.append_event(run_id, "summary_ready", {"count": 2})
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            original = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM audit_events WHERE run_id = ? ORDER BY sequence",
                    (run_id,),
                )
            ]
        forged = copy.deepcopy(original)
        forged[1]["occurred_at_utc"] = "2000-01-01T00:00:00+00:00"
        verification = verify_audit_chain_rows(run_id, forged)
        self.assertFalse(verification.valid)
        self.assertEqual(verification.error_code, "audit_event_hash_mismatch")
        self.assertEqual(verification.error_sequence, 2)
        self.assertTrue(verify_audit_chain_rows(run_id, original).valid)
        self.assertTrue(self.ledger.verify_chain(run_id).valid)

    def test_verify_chain_detects_changed_database_row(self) -> None:
        run_id = self.ledger.start_run(mode="offline", request_summary={"safe": True})
        self.ledger.append_event(run_id, "summary_ready", {"count": 2})
        self.assertTrue(self.ledger.verify_chain(run_id).valid)
        with closing(sqlite3.connect(self.database)) as connection:
            with connection:
                connection.execute("DROP TRIGGER audit_events_no_update")
                connection.execute(
                    "UPDATE audit_events SET occurred_at_utc = ? "
                    "WHERE run_id = ? AND sequence = 2",
                    ("2000-01-01T00:00:00+00:00", run_id),
                )
        verification = self.ledger.verify_chain(run_id)
        self.assertFalse(verification.valid)
        self.assertEqual(verification.error_code, "audit_event_hash_mismatch")
        self.assertEqual(verification.error_sequence, 2)

    def test_export_omits_request_prompt_and_raw_rows(self) -> None:
        prompt_canary = "private-request-content-canary"
        row_canary = "private-row-content-canary"
        run_id = self.ledger.start_run(
            mode="offline",
            request_summary={"prompt": prompt_canary, "records": [{"value": row_canary}]},
        )
        call_id = self.create_call(run_id, {"rows": [{"value": row_canary}]})
        self.approve(call_id)
        attempt_no = self.ledger.start_attempt(call_id)
        result = {"raw_data": row_canary, "aggregate_count": 1}
        self.ledger.finish_attempt_success(
            call_id,
            attempt_no,
            duration_ms=1.0,
            safe_result=result,
            result_hash=sha256_json(result),
        )
        exported = self.ledger.export_run(run_id)
        text = json.dumps(exported, ensure_ascii=False)
        self.assertNotIn(prompt_canary, text)
        self.assertNotIn(row_canary, text)
        self.assertNotIn('"prompt"', text)
        self.assertEqual(
            exported["tool_calls"][0]["safe_args"]["rows"], "[ROW_DATA_OMITTED]"
        )
        self.assertEqual(
            exported["tool_calls"][0]["safe_result"]["raw_data"], "[ROW_DATA_OMITTED]"
        )
        self.assertTrue(exported["chain_verification"]["valid"])

    def test_generic_append_still_rejects_reserved_transport_event(self) -> None:
        run_id = self.ledger.start_run(mode="offline", request_summary={"safe": True})
        with self.assertRaises(AuditError) as caught:
            self.ledger.append_event(run_id, COMPLETION_TRANSPORT_SEND_EVENT, {})
        self.assertEqual(caught.exception.code, "audit_completion_reserved_event_type")
        self.assertEqual(self.ledger.verify_chain(run_id).event_count, 1)


if __name__ == "__main__":
    unittest.main()
