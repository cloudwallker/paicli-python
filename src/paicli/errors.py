"""Public exceptions have sanitized messages suitable for CLI output."""


class PaiCLIError(Exception):
    pass


class PolicyError(PaiCLIError):
    pass


class ApprovalDenied(PolicyError):
    pass


class BudgetExceeded(PaiCLIError):
    pass


class ModelError(PaiCLIError):
    pass


class PlanError(PaiCLIError):
    pass


class PlatformError(PaiCLIError):
    pass
