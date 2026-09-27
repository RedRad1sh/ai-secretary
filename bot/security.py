"""Шифрование секретов (ТЗ §3.4: OAuth-токены хранятся зашифрованными).

Ключ Fernet берётся из ENCRYPTION_KEY; если не задан — генерируется один раз
и сохраняется в data/secret.key (chmod 600).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


def get_fernet(data_dir: Path) -> Fernet:
    key = os.getenv("ENCRYPTION_KEY", "").strip()
    if key:
        return Fernet(key.encode())

    key_file = data_dir / "secret.key"
    if key_file.exists():
        return Fernet(key_file.read_bytes().strip())

    data_dir.mkdir(parents=True, exist_ok=True)
    key_file.write_bytes(Fernet.generate_key())
    os.chmod(key_file, stat.S_IRUSR | stat.S_IWUSR)  # 600
    return Fernet(key_file.read_bytes().strip())


def encrypt_bytes(data_dir: Path, data: bytes) -> bytes:
    return get_fernet(data_dir).encrypt(data)


def decrypt_bytes(data_dir: Path, token: bytes) -> bytes:
    try:
        return get_fernet(data_dir).decrypt(token)
    except InvalidToken as e:
        raise RuntimeError(
            "Не удалось расшифровать токены: ENCRYPTION_KEY не совпадает с тем, "
            "которым шифровали данные. Восстановите ключ или удалите зашифрованный файл."
        ) from e
