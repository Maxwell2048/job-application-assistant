"""Request-scoped dependencies."""
from fastapi import Request


def get_settings(request: Request):
    return request.app.state.settings


def get_worker(request: Request):
    return request.app.state.worker


def get_session(request: Request):
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()
