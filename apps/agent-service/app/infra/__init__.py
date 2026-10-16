"""infra — external-system clients and configuration.

Public surface:
    settings        — app.infra.config.Settings (frozen dataclass)
    get_redis       — app.infra.redis
    mq              — app.infra.rabbitmq
    image_client    — app.infra.image
    lane_router     — app.infra.lane
    lane_policy     — app.infra.lane_policy (the deployment lane, time-based work per lane)
    propagation     — app.infra.propagation (trace / lane between RabbitMQ headers and contextvars)
    RabbitMQManagementClient — app.infra.rabbitmq_management
"""
