import os
import unittest
from unittest.mock import MagicMock, patch
from googleapiclient.errors import HttpError
import httplib2

from read_calendar import get_credentials, get_upcoming_events, main


class TestGoogleCalendarReader(unittest.TestCase):
    def test_get_upcoming_events(self):
        mock_service = MagicMock()
        mock_events = [
            {"summary": "Team Sync", "start": {"dateTime": "2026-11-10T10:00:00Z"}},
            {"summary": "Lunch", "start": {"date": "2026-11-10"}},
        ]
        mock_service.events().list().execute.return_value = {"items": mock_events}

        results = get_upcoming_events(mock_service, max_results=5)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["summary"], "Team Sync")

    def test_get_credentials_missing_secret(self):
        with patch("os.path.exists", return_value=False):
            creds = get_credentials(client_secret_file="nonexistent.json", token_file="nonexistent_token.json")
            self.assertIsNone(creds)

    def test_get_credentials_valid_token(self):
        with patch("os.path.exists", side_effect=lambda p: p == "token.json"):
            with patch("read_calendar.Credentials.from_authorized_user_file") as mock_from_file:
                mock_cred = MagicMock()
                mock_cred.valid = True
                mock_from_file.return_value = mock_cred
                creds = get_credentials(token_file="token.json")
                self.assertIsNotNone(creds)
                self.assertTrue(creds.valid)

    @patch("read_calendar.get_credentials")
    @patch("read_calendar.build")
    @patch("read_calendar.get_upcoming_events")
    def test_main_with_events(self, mock_get_events, mock_build, mock_get_creds):
        mock_get_creds.return_value = MagicMock()
        mock_get_events.return_value = [
            {"summary": "Project Review", "start": {"dateTime": "2026-11-10T14:00:00Z"}},
        ]
        with patch("builtins.print") as mock_print:
            main()
            printed_lines = [call.args[0] for call in mock_print.call_args_list if call.args]
            self.assertTrue(any("Project Review" in line for line in printed_lines))

    @patch("read_calendar.get_credentials")
    @patch("read_calendar.build")
    @patch("read_calendar.get_upcoming_events")
    def test_main_no_events(self, mock_get_events, mock_build, mock_get_creds):
        mock_get_creds.return_value = MagicMock()
        mock_get_events.return_value = []
        with patch("builtins.print") as mock_print:
            main()
            printed_lines = [call.args[0] for call in mock_print.call_args_list if call.args]
            self.assertTrue(any("No upcoming events found." in line for line in printed_lines))

    @patch("read_calendar.get_credentials")
    @patch("read_calendar.build")
    def test_main_http_error(self, mock_build, mock_get_creds):
        mock_get_creds.return_value = MagicMock()
        mock_resp = httplib2.Response({"status": 403, "reason": "Forbidden"})
        mock_build.side_effect = HttpError(resp=mock_resp, content=b"Forbidden")
        with patch("builtins.print") as mock_print:
            main()
            printed_lines = [call.args[0] for call in mock_print.call_args_list if call.args]
            self.assertTrue(any("An error occurred" in line for line in printed_lines))


if __name__ == "__main__":
    unittest.main()
