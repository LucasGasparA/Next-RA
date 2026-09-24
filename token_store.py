"""Guarda o refresh_token cifrado (Fernet) na tabela settings do Postgres.

O valor decriptado nunca é enviado de volta ao navegador (nem em HTML, nem em
JSON) — só transita internamente entre este módulo e main.refresh_access_token.
"""
import os

import psycopg
from cryptography.fernet import Fernet

import store

SETTING_KEY = "refresh_token_enc"


def _fernet() -> Fernet:
    return Fernet(os.environ["ENCRYPTION_KEY"].encode())


def save_refresh_token(conn: psycopg.Connection, token: str) -> None:
    encrypted = _fernet().encrypt(token.encode("utf-8")).decode("ascii")
    store.set_setting(conn, SETTING_KEY, encrypted)


def load_refresh_token(conn: psycopg.Connection) -> str | None:
    encrypted = store.get_setting(conn, SETTING_KEY)
    if encrypted is None:
        return None
    return _fernet().decrypt(encrypted.encode("ascii")).decode("utf-8")


def clear_refresh_token(conn: psycopg.Connection) -> None:
    store.delete_setting(conn, SETTING_KEY)
