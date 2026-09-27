#!/usr/bin/env python3
"""Одноразовая OAuth-авторизация Google (ТЗ §2.4): получение refresh-токена.

Использование:
  1. Скачайте client_secret.json из Google Cloud Console (docs/SETUP_GOOGLE.md).
  2. python scripts/gcal_auth.py
  3. Скопируйте строку GOOGLE_REFRESH_TOKEN=... в .env

Токен шифруется ключом ENCRYPTION_KEY и сохраняется в data/google_tokens.bin,
но .env — основной источник (проще переносить между машинами).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402

from bot.security import encrypt_bytes  # noqa: E402

SCOPES = ["https://www.googleapis.com/auth/calendar"]
BASE_DIR = Path(__file__).resolve().parent.parent


def main() -> None:
    secret = BASE_DIR / "client_secret.json"
    if not secret.exists():
        print("❌ Нет client_secret.json рядом со скриптом (см. docs/SETUP_GOOGLE.md)")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")

    print("\n✅ Успешно! Добавьте строку в .env:\n")
    print(f"GOOGLE_CLIENT_ID={creds.client_id}")
    print(f"GOOGLE_CLIENT_SECRET={creds.client_secret}")
    print(f"GOOGLE_REFRESH_TOKEN={creds.refresh_token}\n")

    # Дополнительно сохраняем зашифрованную копию (ТЗ §3.4)
    data_dir = BASE_DIR / "data"
    try:
        blob = encrypt_bytes(
            data_dir,
            (f"{creds.client_id}\n{creds.client_secret}\n{creds.refresh_token}").encode(),
        )
        (data_dir / "google_tokens.bin").write_bytes(blob)
        print(f"🔒 Зашифрованная копия: data/google_tokens.bin")
    except Exception as e:  # noqa: BLE001
        print(f"(копию сохранить не удалось: {e} — .env достаточно)")


if __name__ == "__main__":
    main()
