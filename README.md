# SmetaProAI — СН-2012 на 01.07.2026

Официальный сборник стоимостных нормативов Москвы (СН-2012, выпуск 8 / «СН-2026») в текущих ценах **на 01.07.2026**.

Утверждён распоряжением Департамента экономической политики и развития города Москвы от 25.06.2026 № ДПР-Р-15/26.

## Источник

- Страница документа: https://www.mos.ru/depr/documents/cenovaya-politika/cenoobrazovanie-v-gorodskom-khozyaistve/cenoobrazovanie-v-gorodskom-khozyaistve-2026-god/view/344770220/
- Портал сборника: https://ac.mos.ru/cn-2012/
- PDF скачаны с `www.mos.ru/upload/documents/files/...`

Это публичные PDF с mos.ru. Коммерческие базы в формате Smeta.RU / Гранд-Смета сюда не входят.

## Что лежит в проекте

| Путь | Содержимое |
| --- | --- |
| `data/sn2012_2026.sqlite` | Рабочая SQLite-база |
| `data/raw/pdf/` | 97 официальных PDF (~262 МБ) |
| `data/raw/manifest.json` | Список файлов, URL, SHA-256 |
| `scripts/download_sn2026.py` | Повторная загрузка с mos.ru |
| `scripts/build_db.py` | Разбор PDF → SQLite |
| `scripts/search.py` | Поиск по расценкам / материалам / машинам |

## Схема базы

- `documents` — файлы сборника
- `rates` — стоимостные нормативы (шифр, наименование, измеритель, прямые затраты, зарплата, машины, материалы, трудозатраты)
- `rate_resources` — ресурсы в составе расценки
- `materials` — глава 21, материалы
- `machines` — глава 22, машины и механизмы
- `pages` — полный текст каждой страницы PDF
- `rates_fts` — полнотекстовый поиск

Шифр внутри сборника может повторяться. Уникальный ключ: `full_code`, например `СН-2012.1-1:1-3101-1-1/1`.

## Примеры

```powershell
python scripts/search.py "1-3101-1-1/1"
python scripts/search.py отопление
python scripts/search.py битум --kind materials
python scripts/search.py экскаватор --kind machines
```

```sql
SELECT full_code, name, unit, direct_cost, wages, labor_hours
FROM rates
WHERE name LIKE '%грунт%' AND direct_cost IS NOT NULL
LIMIT 20;
```

Пересобрать базу после повторной загрузки:

```powershell
python scripts/download_sn2026.py
python scripts/build_db.py
```

SQLite лежит в Git LFS (файл больше 100 МБ). После клонирования нужен Git LFS:

```powershell
git lfs install
git lfs pull
```
