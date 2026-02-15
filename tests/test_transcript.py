"""Tests for transcript JSON schema validation and save logic."""

import json
import pytest
from datetime import datetime
from pathlib import Path

TRANSCRIPTS_DIR = Path(__file__).parent.parent / "transcripts"


class TestTranscriptSchema:
    """Validate transcript JSON structure."""

    REQUIRED_TOP_KEYS = {"store_name", "ac_model", "room", "phone", "timestamp", "messages"}
    REQUIRED_MESSAGE_KEYS = {"role", "text", "time"}

    def test_fixture_has_required_keys(self, sample_transcript_data):
        assert self.REQUIRED_TOP_KEYS.issubset(sample_transcript_data.keys())

    def test_messages_have_required_keys(self, sample_transcript_data):
        for msg in sample_transcript_data["messages"]:
            assert self.REQUIRED_MESSAGE_KEYS.issubset(msg.keys())

    def test_message_roles_valid(self, sample_transcript_data):
        valid_roles = {"user", "assistant", "system"}
        for msg in sample_transcript_data["messages"]:
            assert msg["role"] in valid_roles

    def test_timestamp_is_iso_format(self, sample_transcript_data):
        datetime.fromisoformat(sample_transcript_data["timestamp"])

    def test_message_times_are_iso(self, sample_transcript_data):
        for msg in sample_transcript_data["messages"]:
            datetime.fromisoformat(msg["time"])

    def test_phone_format(self, sample_transcript_data):
        phone = sample_transcript_data["phone"]
        assert phone == "browser" or phone.startswith("+")


class TestExistingTranscripts:
    """Validate real transcript files on disk."""

    def _get_transcript_files(self):
        if not TRANSCRIPTS_DIR.exists():
            return []
        return [f for f in TRANSCRIPTS_DIR.glob("*.json")
                if not f.name.endswith('.analysis.json')]

    def test_transcripts_are_valid_json(self):
        files = self._get_transcript_files()
        if not files:
            pytest.skip("No transcript files found")
        for f in files:
            data = json.loads(f.read_text())
            assert "store_name" in data
            assert "messages" in data
            assert isinstance(data["messages"], list)

    def test_transcripts_have_messages(self):
        files = self._get_transcript_files()
        if not files:
            pytest.skip("No transcript files found")
        for f in files:
            data = json.loads(f.read_text())
            assert len(data["messages"]) > 0, f"Empty transcript: {f.name}"


class TestTranscriptSaveLogic:
    """Test the save logic by replicating what _save_transcript does."""

    def test_save_and_reload(self, tmp_path, sample_transcript_data):
        filename = tmp_path / "test_transcript.json"
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(sample_transcript_data, f, ensure_ascii=False, indent=2)
        loaded = json.loads(filename.read_text(encoding="utf-8"))
        assert loaded == sample_transcript_data

    def test_ensure_ascii_false_preserves_hindi(self, tmp_path):
        data = {"messages": [{"role": "assistant", "text": "ए सी का रेट", "time": "2026-01-01T00:00:00"}]}
        filename = tmp_path / "hindi_test.json"
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        raw = filename.read_text(encoding="utf-8")
        assert "ए सी" in raw  # Should NOT be escaped to \uXXXX

    def test_filename_format(self):
        store_name = "Pai International Jayanagar"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{store_name.replace(' ', '_')}_{ts}.json"
        assert " " not in filename
        assert filename.endswith(".json")
