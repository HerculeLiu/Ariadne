class AriadneError(Exception):
    code = 10000
    message = "unknown error"

    def __init__(self, message=None, field=None, reason=None):
        super().__init__(message or self.message)
        self.message = message or self.message
        self.field = field
        self.reason = reason


class ValidationError(AriadneError):
    code = 10001
    message = "validation failed"


class NotFoundError(AriadneError):
    code = 10003
    message = "resource not found"


class VersionConflictError(AriadneError):
    code = 10005
    message = "version conflict"


class UnsupportedFileTypeError(AriadneError):
    code = 10006
    message = "unsupported file type"


class FileSizeLimitError(AriadneError):
    code = 10007
    message = "file size exceeds limit"


class LLMServiceError(AriadneError):
    code = 10010
    message = "llm generation failed"
