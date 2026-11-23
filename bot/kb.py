"""База знаний и стиль: рабочие копии в DATA_DIR/kb, дефолт из образа (kb/ рядом с пакетом)."""
from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

NAMES = ("knowledge.md", "style.md")
MAX_BYTES = 512 * 1024


class KBError(ValueError):
    """Файл не подходит: не тот, пустой, слишком большой или не UTF-8."""


class KB:
    def __init__(self, work_dir: Path, default_dir: Path) -> None:
        self.dir = work_dir
        self.default_dir = default_dir

    @property
    def backup_dir(self) -> Path:
        return self.dir / "backup"

    def ensure(self) -> None:
        """Нет рабочей копии: берём дефолт из образа. Нет и его: работаем без файла."""
        self.dir.mkdir(parents=True, exist_ok=True)
        for name in NAMES:
            target = self.dir / name
            if target.exists():
                continue
            src = self.default_dir / name
            if src.is_file():
                shutil.copyfile(src, target)
                log.info("kb: %s скопирован из дефолта", name)
            else:
                log.warning("kb: нет ни рабочего, ни дефолтного %s", name)

    def path(self, name: str) -> Path:
        if name not in NAMES:
            raise KBError(name)
        return self.dir / name

    def read(self, name: str) -> str:
        p = self.path(name)
        if not p.is_file():
            src = self.default_dir / name
            if not src.is_file():
                return ""
            p = src
        try:
            return p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            log.error("kb: не прочитался %s: %s", name, type(e).__name__)
            return ""

    def head(self, name: str, lines: int = 8) -> list[str]:
        text = self.read(name)
        return [ln for ln in text.splitlines() if ln.strip()][:lines]

    def line_count(self, name: str) -> int:
        return len(self.read(name).splitlines())

    @staticmethod
    def normalize_name(file_name: str | None) -> str | None:
        if not file_name:
            return None
        low = Path(file_name).name.strip().lower()
        return low if low in NAMES else None

    def replace(self, name: str, data: bytes, now: datetime) -> tuple[int, Path | None]:
        """Заменить файл: прошлая версия уходит в backup/. Возвращает (строк, путь бэкапа)."""
        if name not in NAMES:
            raise KBError("Нужен файл knowledge.md или style.md")
        if len(data) > MAX_BYTES:
            raise KBError("Файл больше 512 КБ")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise KBError("Файл не в UTF-8") from None
        if not text.strip():
            raise KBError("Файл пустой")
        self.dir.mkdir(parents=True, exist_ok=True)
        target = self.dir / name
        backup: Path | None = None
        if target.exists():
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = now.strftime("%Y%m%d-%H%M%S")
            backup = self.backup_dir / f"{Path(name).stem}-{stamp}.md"
            n = 1
            while backup.exists():
                n += 1
                backup = self.backup_dir / f"{Path(name).stem}-{stamp}-{n}.md"
            shutil.copyfile(target, backup)
        tmp = target.with_suffix(".md.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
        return len(text.splitlines()), backup
