"""Environment configuration. Nothing here is a credential to another system."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


@dataclass(frozen=True)
class Config:
    secret_key: str
    store_ingest_key: str
    database_url: str
    base_url: str
    mail_backend: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    mail_from: str
    login_token_minutes: int
    graph_tenant_id: str = ""
    graph_client_id: str = ""
    graph_client_secret: str = ""
    graph_sender_mailbox: str = ""

    @property
    def testing_defaults(self) -> bool:
        return not self.secret_key


def load_config() -> Config:
    db = _env("DATABASE_URL")
    if db.startswith("postgres://"):            # Render's legacy scheme
        db = "postgresql+psycopg://" + db[len("postgres://"):]
    elif db.startswith("postgresql://"):
        db = "postgresql+psycopg://" + db[len("postgresql://"):]
    return Config(
        secret_key=_env("SECRET_KEY"),
        store_ingest_key=_env("STORE_INGEST_KEY"),
        database_url=db or "sqlite:///store.db",
        base_url=_env("BASE_URL", "http://localhost:5000").rstrip("/"),
        mail_backend=_env("MAIL_BACKEND", "log").lower(),
        smtp_host=_env("SMTP_HOST"),
        smtp_port=int(_env("SMTP_PORT", "587") or 587),
        smtp_user=_env("SMTP_USER"),
        smtp_password=_env("SMTP_PASSWORD"),
        mail_from=_env("MAIL_FROM", "closeouts@gersoncompany.com"),
        login_token_minutes=int(_env("LOGIN_TOKEN_MINUTES", "30") or 30),
        graph_tenant_id=_env("GRAPH_TENANT_ID"),
        graph_client_id=_env("GRAPH_CLIENT_ID"),
        graph_client_secret=_env("GRAPH_CLIENT_SECRET"),
        graph_sender_mailbox=_env("GRAPH_SENDER_MAILBOX"),
    )
