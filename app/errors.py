"""统一业务错误。所有错误响应格式：
{"error": {"code": "...", "message": "...", "details": {...}}}
"""
from typing import Any, Dict, Optional


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}
