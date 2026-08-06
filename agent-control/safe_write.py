# -*- coding: utf-8 -*-
"""Безопасная запись патчей.

🔴 Куплено ошибкой 06.08.2026: `io.open(p, "w", newline="\n")` с неверным
значением newline УСПЕЛ обрезать файл до того, как выбросил исключение, — и
координатор уехал на сервер пустым. Проверка `ast.parse("")` при этом молчит:
пустая строка разбирается успешно. Поэтому здесь: сначала разбор, потом запрет
на пустой результат, и только затем запись во временный файл с заменой.
"""
import ast
import os


def save(path, text, min_bytes=500):
    if not text or len(text.encode("utf-8")) < min_bytes:
        raise SystemExit(f"отказ: новое содержимое {path} подозрительно мало "
                         f"({len(text)} символов) — файл не тронут")
    ast.parse(text)
    tmp = path + ".tmp-patch"
    with open(tmp, "w", encoding="utf-8", newline=chr(10)) as f:
        f.write(text)
    if os.path.getsize(tmp) < min_bytes:
        os.unlink(tmp)
        raise SystemExit("отказ: временный файл пуст — исходный не тронут")
    os.replace(tmp, path)
    print(f"записан {path}: {len(text)} символов")
