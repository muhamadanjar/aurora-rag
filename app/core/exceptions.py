class DomainError(Exception):
    """Base class for errors that can be translated by the delivery layer."""


class InvalidInput(DomainError):
    pass


class DatasetNotFound(DomainError):
    pass


class ExportNotFound(DomainError):
    pass


class ExportNotReady(DomainError):
    pass


class ExportExpired(DomainError):
    pass


class ExportEmpty(DomainError):
    pass


class ConfigurationInvalid(DomainError):
    pass


class StoragePathViolation(DomainError):
    pass


class InvalidCredentials(DomainError):
    pass


class TooManyLoginAttempts(DomainError):
    pass


class ResourceConflict(DomainError):
    pass


class PreconditionFailed(DomainError):
    pass


class PreconditionRequired(DomainError):
    pass


class ChatUnavailable(DomainError):
    pass
