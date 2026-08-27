class WearableLogsError(RuntimeError):
    """Actionable user-facing failure."""


class AdbError(WearableLogsError):
    """ADB invocation or device-state failure."""


class OcrError(WearableLogsError):
    """OCR initialization or execution failure."""


class ConfigurationError(WearableLogsError):
    """Invalid configuration or command input."""
