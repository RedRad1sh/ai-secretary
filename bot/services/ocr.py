"""Local OCR: no cloud credentials; bounded file size and subprocess lifetime."""
import asyncio
import tempfile
from pathlib import Path


async def recognize_image(image: bytes) -> str:
    if len(image) > 5 * 1024 * 1024:
        raise ValueError("Изображение больше 5 МБ")
    with tempfile.TemporaryDirectory(prefix="secretary-ocr-") as directory:
        path = Path(directory) / "image.jpg"
        path.write_bytes(image)
        try:
            proc = await asyncio.create_subprocess_exec(
                "tesseract", str(path), "stdout", "-l", "rus+eng",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise ValueError("OCR не установлен: нужны tesseract и языки rus+eng") from e
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), 20)
        except BaseException:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            raise
        if proc.returncode:
            raise ValueError("Не удалось прочитать изображение")
        return stdout.decode("utf-8", errors="replace").strip()[:12000]
