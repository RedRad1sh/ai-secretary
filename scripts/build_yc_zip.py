#!/usr/bin/env python3
"""Собирает yc-function.zip для загрузки в Yandex Cloud Functions (docs/DEPLOY_YC.md).

В архив входят: пакет bot/, точка входа yandex_handler.py, requirements.txt
(зависимости YC установит сам при создании версии).
"""

from __future__ import annotations

import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "yc-function.zip"


def main() -> None:
    files: list[Path] = []
    files += sorted((BASE / "bot").rglob("*.py"))
    files.append(BASE / "serverless" / "yandex_handler.py")
    files.append(BASE / "requirements.txt")

    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            rel = f.relative_to(BASE)
            arcname = rel if rel.parts[0] == "bot" else Path(f.name)
            z.write(f, arcname)
            print(f"  + {arcname}")

    size_kb = OUT.stat().st_size / 1024
    print(f"\nГотово: {OUT.name} ({size_kb:.0f} КБ). Загружайте его в Cloud Functions.")


if __name__ == "__main__":
    main()
