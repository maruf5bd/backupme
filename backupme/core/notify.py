"""Failure notification dispatcher (backlog B16).

Consumes the previously-unused NotificationsConfig (on_failure / desktop):
every failure is ALWAYS logged; a desktop toast is best-effort when enabled.
Never raises — a notification problem must not break the failing pipeline.
"""
from backupme.log import get_logger

log = get_logger("core.notify")


def _notif_settings(config) -> tuple[bool, bool]:
    """(on_failure, desktop) from a dict or dataclass config; safe defaults True."""
    section = None
    if config is None:
        return True, True
    if isinstance(config, dict):
        section = config.get("notifications", {}) or {}
        get = section.get
    else:
        section = getattr(config, "notifications", None)
        if section is None:
            return True, True
        get = lambda k, d=None: getattr(section, k, d)  # noqa: E731
    return bool(get("on_failure", True)), bool(get("desktop", True))


def _desktop_toast(title: str, message: str) -> None:
    """Best-effort Windows toast via plyer; ImportError/any error -> log only."""
    from plyer import notification

    notification.notify(title=title[:64], message=message[:200], timeout=5)


def notify_failure(title: str, detail: str, config=None) -> dict:
    """Report a failure: always to the log; desktop toast when configured.

    Returns {"logged": True, "desktop": "sent"|"skipped-disabled"|"unavailable"|"error"}.
    """
    log.warning("%s: %s", title, detail)
    on_failure, desktop = _notif_settings(config)
    if not on_failure or not desktop:
        log.info("desktop notification skipped (disabled in config)")
        return {"logged": True, "desktop": "skipped-disabled"}
    try:
        _desktop_toast(title, detail)
        return {"logged": True, "desktop": "sent"}
    except ImportError:
        log.info("desktop notification unavailable (plyer not installed)")
        return {"logged": True, "desktop": "unavailable"}
    except Exception:
        log.warning("desktop notification failed", exc_info=True)
        return {"logged": True, "desktop": "error"}
