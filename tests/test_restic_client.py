"""Unit tests for restic_client.py's pure-logic pieces: JSON parsing and
argument-building. Uses mocked subprocess.run so no real restic binary is
required to run these tests."""

import json
import subprocess
import unittest
from unittest.mock import patch, MagicMock

import restic_client
from restic_client import ResticError


def _completed(stdout="", returncode=0):
    return MagicMock(stdout=stdout, stderr="", returncode=returncode)


class TestParseJsonOutput(unittest.TestCase):
    def test_single_json_value(self):
        result = restic_client.parse_json_output('{"a": 1}')
        self.assertEqual(result, {"a": 1})

    def test_newline_delimited_json(self):
        stdout = '{"a": 1}\n{"b": 2}\n'
        result = restic_client.parse_json_output(stdout)
        self.assertEqual(result, [{"a": 1}, {"b": 2}])

    def test_empty_output(self):
        self.assertIsNone(restic_client.parse_json_output(""))
        self.assertIsNone(restic_client.parse_json_output("   \n  "))


class TestRunRestic(unittest.TestCase):
    @patch("restic_client.subprocess.run")
    def test_raises_resticerror_on_failure(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(
            returncode=1, cmd=["restic"], stderr="repository not found"
        )
        with self.assertRaises(ResticError) as ctx:
            restic_client.run_restic(["snapshots"], "/tmp/repo")
        self.assertIn("repository not found", str(ctx.exception))

    @patch("restic_client.subprocess.run")
    def test_raises_resticerror_when_binary_missing(self, mock_run):
        mock_run.side_effect = FileNotFoundError()
        with self.assertRaises(ResticError):
            restic_client.run_restic(["snapshots"], "/tmp/repo")

    @patch("restic_client.subprocess.run")
    def test_password_file_takes_precedence(self, mock_run):
        mock_run.return_value = _completed()
        restic_client.run_restic(
            ["snapshots"], "/tmp/repo", password="ignored", password_file="/tmp/pw"
        )
        env = mock_run.call_args.kwargs["env"]
        self.assertEqual(env["RESTIC_PASSWORD_FILE"], "/tmp/pw")
        self.assertNotIn("RESTIC_PASSWORD", env)


class TestBackup(unittest.TestCase):
    @patch("restic_client.subprocess.run")
    def test_parses_summary_from_json_events(self, mock_run):
        events = [
            {"message_type": "status", "percent_done": 0.5},
            {"message_type": "summary", "snapshot_id": "abc123", "files_new": 3,
             "data_added": 1024, "total_bytes_processed": 2048},
        ]
        stdout = "\n".join(json.dumps(e) for e in events)
        mock_run.return_value = _completed(stdout=stdout)

        summary = restic_client.backup("/tmp/repo", ["/home/jim"], tags=["job1"])
        self.assertEqual(summary["snapshot_id"], "abc123")
        self.assertEqual(summary["files_new"], 3)

        args = mock_run.call_args.args[0]
        self.assertIn("--tag", args)
        self.assertIn("job1", args)
        self.assertIn("/home/jim", args)

    @patch("restic_client.subprocess.run")
    def test_raises_if_no_summary_event(self, mock_run):
        stdout = json.dumps({"message_type": "status", "percent_done": 0.1})
        mock_run.return_value = _completed(stdout=stdout)
        with self.assertRaises(ResticError):
            restic_client.backup("/tmp/repo", ["/home/jim"])


class TestForgetPrune(unittest.TestCase):
    @patch("restic_client.subprocess.run")
    def test_builds_keep_and_tag_flags(self, mock_run):
        mock_run.return_value = _completed()
        restic_client.forget_prune(
            "/tmp/repo", keep_daily=7, keep_weekly=4, keep_monthly=0,
            keep_yearly=0, tags=["job1"],
        )
        args = mock_run.call_args.args[0]
        self.assertIn("--prune", args)
        self.assertIn("--keep-daily", args)
        self.assertIn("7", args)
        self.assertIn("--keep-weekly", args)
        self.assertIn("4", args)
        self.assertNotIn("--keep-monthly", args)
        self.assertIn("--tag", args)
        self.assertIn("job1", args)


class TestRestore(unittest.TestCase):
    @patch("restic_client.subprocess.run")
    def test_includes_target_and_include_path(self, mock_run):
        mock_run.return_value = _completed()
        restic_client.restore("/tmp/repo", "abc123", "/tmp/dest", include_path="/tmp/example/file.txt")
        args = mock_run.call_args.args[0]
        self.assertIn("restore", args)
        self.assertIn("abc123", args)
        self.assertIn("--target", args)
        self.assertIn("/tmp/dest", args)
        self.assertIn("--include", args)
        self.assertIn("/tmp/example/file.txt", args)


class TestCheck(unittest.TestCase):
    @patch("restic_client.subprocess.run")
    def test_default_is_metadata_only(self, mock_run):
        mock_run.return_value = _completed()
        restic_client.check("/tmp/repo")
        args = mock_run.call_args.args[0]
        self.assertIn("check", args)
        self.assertNotIn("--read-data", args)
        self.assertFalse(any(a.startswith("--read-data-subset") for a in args))

    @patch("restic_client.subprocess.run")
    def test_read_data_adds_flag(self, mock_run):
        mock_run.return_value = _completed()
        restic_client.check("/tmp/repo", read_data=True)
        args = mock_run.call_args.args[0]
        self.assertIn("--read-data", args)

    @patch("restic_client.subprocess.run")
    def test_read_data_subset_takes_precedence(self, mock_run):
        mock_run.return_value = _completed()
        restic_client.check("/tmp/repo", read_data=True, read_data_subset="5%")
        args = mock_run.call_args.args[0]
        self.assertIn("--read-data-subset=5%", args)
        self.assertNotIn("--read-data", args)

    @patch("restic_client.subprocess.run")
    def test_raises_resticerror_on_failure(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(
            returncode=1, cmd=["restic"], stderr="unable to load pack"
        )
        with self.assertRaises(ResticError):
            restic_client.check("/tmp/repo")


if __name__ == "__main__":
    unittest.main()
