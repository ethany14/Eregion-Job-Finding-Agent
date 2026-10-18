"""Domain errors for governed memory persistence."""


class MemoryRuntimeError(RuntimeError):
    pass


class MemoryNotFoundError(MemoryRuntimeError):
    pass


class MemoryAlreadyExistsError(MemoryRuntimeError):
    pass


class StaleMemoryError(MemoryRuntimeError):
    pass


class InvalidMemoryTransitionError(MemoryRuntimeError):
    pass


class MemoryConfirmationRequiredError(InvalidMemoryTransitionError):
    pass


class MemoryOwnerMismatchError(MemoryRuntimeError):
    pass


class MemoryReplacementNotValidError(InvalidMemoryTransitionError):
    """The proposed replacement does not hold at the time of confirmation."""
