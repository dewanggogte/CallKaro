"""Tests for per-call log saving — _setup_call_logger."""

import logging
import os
from pathlib import Path

from tests.conftest import _setup_call_logger


class TestSetupCallLogger:
    def test_creates_log_file(self):
        handler, log_path = _setup_call_logger("Test Store")
        try:
            assert Path(log_path).exists()
            assert "Test_Store" in log_path
            assert log_path.endswith(".log")
        finally:
            logging.getLogger().removeHandler(handler)
            handler.close()
            os.unlink(log_path)

    def test_handler_attached_to_root(self):
        handler, log_path = _setup_call_logger("Test Store 2")
        try:
            assert handler in logging.getLogger().handlers
        finally:
            logging.getLogger().removeHandler(handler)
            handler.close()
            os.unlink(log_path)

    def test_handler_level_is_debug(self):
        handler, log_path = _setup_call_logger("Test Store 3")
        try:
            assert handler.level == logging.DEBUG
        finally:
            logging.getLogger().removeHandler(handler)
            handler.close()
            os.unlink(log_path)

    def test_log_messages_written_to_file(self):
        handler, log_path = _setup_call_logger("Test Store 4")
        try:
            test_logger = logging.getLogger("test.log.write")
            test_logger.setLevel(logging.DEBUG)
            test_logger.info("Test message for log file")
            handler.flush()
            content = Path(log_path).read_text()
            assert "Test message for log file" in content
        finally:
            logging.getLogger().removeHandler(handler)
            handler.close()
            os.unlink(log_path)

    def test_store_name_spaces_replaced(self):
        handler, log_path = _setup_call_logger("Pai International Jayanagar")
        try:
            assert "Pai_International_Jayanagar" in log_path
        finally:
            logging.getLogger().removeHandler(handler)
            handler.close()
            os.unlink(log_path)

    def test_cleanup_removes_handler(self):
        handler, log_path = _setup_call_logger("Cleanup Test")
        root = logging.getLogger()
        assert handler in root.handlers
        root.removeHandler(handler)
        handler.close()
        assert handler not in root.handlers
        os.unlink(log_path)
