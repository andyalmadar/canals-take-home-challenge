from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.services.orders import OrderService


def get_order_service(request: Request) -> OrderService:
    service: OrderService = request.app.state.order_service
    return service


OrderServiceDep = Annotated[OrderService, Depends(get_order_service)]
