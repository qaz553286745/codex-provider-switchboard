import logging
import os
import stat

from codex_provider_switchboard.infrastructure.log_history import (
    configure_log_history,
)


def test_log_history_rotates_and_redacts_protocol_and_credentials(tmp_path) -> None:
    log_path = tmp_path / "private-logs" / "switchboard.log"
    root = logging.getLogger()
    try:
        configure_log_history(
            path=log_path,
            level="info",
            max_bytes=512,
            backup_count=2,
        )
        logger = logging.getLogger("codex_provider_switchboard.test")
        for index in range(30):
            logger.info("safe rotation event=%d padding=%s", index, "x" * 48)
        logger.info(
            "api_key=%s Authorization: Bearer %s marker=%s identifier=%s",
            "crsr_example_secret_123456789",
            "bearer-example-secret-123456789",
            "CODEX_SWITCHBOARD_BRIDGE_BEGIN_deadbeef",
            "123e4567-e89b-42d3-a456-426614174000",
        )
        for handler in root.handlers:
            handler.flush()

        files = sorted(log_path.parent.glob("switchboard.log*"))
        combined = "".join(path.read_text(encoding="utf-8") for path in files)
        assert len(files) > 1
        assert "crsr_example_secret_123456789" not in combined
        assert "bearer-example-secret-123456789" not in combined
        assert "CODEX_SWITCHBOARD_BRIDGE_BEGIN_deadbeef" not in combined
        assert "123e4567-e89b-42d3-a456-426614174000" not in combined
        assert "redacted" in combined
        assert stat.S_IMODE(log_path.parent.stat().st_mode) == 0o700
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in files)
    finally:
        for handler in list(root.handlers):
            if getattr(handler, "_switchboard_history", False):
                root.removeHandler(handler)
                handler.close()


def test_log_history_does_not_chmod_an_existing_parent(tmp_path) -> None:
    log_dir = tmp_path / "shared-logs"
    log_dir.mkdir(mode=0o755)
    os.chmod(log_dir, 0o755)  # noqa: S103 - verify shared parent preservation
    log_path = log_dir / "switchboard.log"
    root = logging.getLogger()
    try:
        configure_log_history(
            path=log_path, level="info", max_bytes=512, backup_count=1
        )
        logging.getLogger("codex_provider_switchboard.test").info("safe event")
        for handler in root.handlers:
            handler.flush()
        assert stat.S_IMODE(log_dir.stat().st_mode) == 0o755
        assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    finally:
        for handler in list(root.handlers):
            if getattr(handler, "_switchboard_history", False):
                root.removeHandler(handler)
                handler.close()
