# -*- coding: utf-8 -*-
"""Кириллица ↔ латиница для поиска.

Рувим диктует голосом, и латинские названия приходят кириллицей: «волмарт»,
«амекс», «аэроплан», «гудиер». В записях они стоят как Walmart, Amex,
Aeroplan, Goodyear — и поиск по словам их не связывает, а смысловой на
коротких запросах путается.

Здесь дешёвое сопоставление: слово переводится в латиницу по таблице, а для
частых брендов есть прямые соответствия — транслитерация «эмекс» до «amex»
сама по себе не доходит.

Ничего не угадывается наверняка: варианты просто добавляются к запросу как
дополнительные кандидаты, поэтому ложное соответствие максимум добавит шум,
а не отменит правильную находку.
"""

import re

_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

# Названия, которые на слух записываются не по буквам. Ключ — как слышится,
# значение — как пишется в записях.
BRANDS = {
    "волмарт": "walmart", "волмерт": "walmart", "уолмарт": "walmart",
    "амекс": "amex", "эмекс": "amex", "американ": "american",
    "аэроплан": "aeroplan", "аэроплана": "aeroplan",
    "гудиер": "goodyear", "гудьир": "goodyear",
    "хилтон": "hilton", "мариотт": "marriott", "марриотт": "marriott",
    "чейз": "chase", "дискавер": "discover", "капитал": "capital",
    "хоумвуд": "homewood", "хэмптон": "hampton", "эмбасси": "embassy",
    "мертл": "myrtle", "мёртл": "myrtle", "бич": "beach",
    "додж": "dodge", "караван": "caravan", "ниссан": "nissan",
    "сентра": "sentra", "тойота": "toyota", "хонда": "honda",
    "хетзнер": "hetzner", "хетцнер": "hetzner",
    "телеграм": "telegram", "гугл": "google", "амазон": "amazon",
    "питон": "python", "клод": "claude", "джипити": "gpt",
}

_CYR = re.compile(r"[а-яё]", re.I)


def is_cyrillic(word):
    return bool(_CYR.search(word))


def to_latin(word):
    """Побуквенная транслитерация: «караван» → «karavan»."""
    return "".join(_MAP.get(c, c) for c in word.lower())


def variants(word):
    """Варианты написания слова, которые стоит поискать дополнительно."""
    w = word.lower()
    if not is_cyrillic(w):
        return []
    out = []
    if w in BRANDS:
        out.append(BRANDS[w])
    else:
        # ищем известный бренд по началу слова (падежи: «волмарте», «амексом»)
        for k, v in BRANDS.items():
            if w.startswith(k[:max(4, len(k) - 2)]):
                out.append(v)
                break
    lat = to_latin(w)
    if lat and lat != w:
        out.append(lat)
    return list(dict.fromkeys(out))


def expand(query):
    """Все дополнительные варианты для слов запроса."""
    out = []
    for w in re.findall(r"\w{3,}", query, re.U):
        out.extend(variants(w))
    return list(dict.fromkeys(out))
