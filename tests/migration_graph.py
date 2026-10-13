# -*- coding: utf-8 -*-
"""
هلپر مشترک تست‌ها: خواندن گراف migrationهای Alembic از روی فایل‌ها.

چرا لازم شد: چند تست «سرِ migrationها» را با عدد ثابت (`0003`) مقایسه می‌کردند؛
با هر migration تازه، آن تست‌ها بی‌دلیل می‌شکستند (و کسی برای سبز کردن، عدد را
به‌روز می‌کرد — یعنی تست چیزی را نگهبانی نمی‌کرد). این هلپر سر را **از روی فایل‌ها**
محاسبه می‌کند تا قفل‌ها معنادار بمانند: «اسکیمای اجراشده = سرِ واقعیِ زنجیره».
"""
from pathlib import Path

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "alembic" / "versions"


def _field(lines, name):
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{name} ="):
            return stripped.split("=", 1)[1].strip().strip("\"'")
    return None


def migration_graph():
    """``{revision: down_revision}`` — فقط از متغیرهای سطح‌ماژول فایل‌ها."""
    graph = {}
    for path in sorted(VERSIONS_DIR.glob("0*.py")):
        revision = _field(path.read_text(encoding="utf-8").splitlines(), "revision")
        parent = _field(path.read_text(encoding="utf-8").splitlines(), "down_revision")
        if not revision:
            raise AssertionError(f"{path.name}: متغیر revision ندارد")
        graph[revision] = None if parent in (None, "None", "") else parent
    return graph


def migration_head() -> str:
    """سرِ یکتای زنجیره؛ اگر بیش از یک سر باشد (شاخه‌شدن) آزمون می‌شکند."""
    graph = migration_graph()
    heads = set(graph) - {p for p in graph.values() if p}
    if len(heads) != 1:
        raise AssertionError(
            f"انتظار یک سر داشتیم، شد: {sorted(heads)} (گراف: {graph})")
    return heads.pop()
