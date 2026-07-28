class ModelGatewayError(RuntimeError):
    code = "service_error"


class ModelConfigurationError(ModelGatewayError):
    code = "configuration_error"


class ModelAuthenticationError(ModelGatewayError):
    code = "authentication_error"


class ModelRateLimitError(ModelGatewayError):
    code = "rate_limit_error"


class ModelTimeoutError(ModelGatewayError):
    code = "timeout_error"


class ModelProtocolError(ModelGatewayError):
    code = "protocol_error"


class ModelServiceError(ModelGatewayError):
    code = "service_error"
