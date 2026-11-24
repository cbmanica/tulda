"""Error types; each carries the CLI exit code from the plan."""


class TuldaError(Exception):
    code = 1


class ConfigError(TuldaError):
    code = 1


class NoAdapter(TuldaError):
    code = 2


class MissingRequirement(TuldaError):
    code = 3


class StepFailed(TuldaError):
    code = 4


class StepTimeout(TuldaError):
    code = 5
