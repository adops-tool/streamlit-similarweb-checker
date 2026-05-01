# DataLoom Traffic Checker

Bulk SimilarWeb traffic lookup via the [DataLoom RapidAPI](https://rapidapi.com/) provider, wrapped in a Streamlit UI. Designed as part of an ad-tech lead-generation pipeline (Agent-1) where domain lists are filtered by `ads.txt` / `app-ads.txt` checks and traffic thresholds before contact enrichment.

## What's new in this version

| Area | Before | After |
|------|--------|-------|
| HTTP | Sync, `time.sleep(0.35)` between calls | `aiohttp` + `asyncio.Semaphore(3)` — ~3× faster, still under the rate limit |
| Failures | One try, network error → "-" | Up to 3 attempts with exponential backoff |
| Resume | None — losing the tab loses everything | Per-domain checkpoint in `st.session_state`, idempotent re-runs |
| Key check | First domain reveals a bad key | One-click pre-flight validation against `google.com` |
| Quota waste | Duplicate URLs lookup twice | Input deduplicated after normalization |
| Response parsing | Recursive search for any "visits"-like key | Explicit priority list, predictable result |
| URL cleaning | Schema, www, path | Adds trailing-dot, port, fragment, mobile-prefix handling |
| Output | Full list only | Optional minimum-visits threshold filter applied to the download |

## Quick start

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

Open the printed URL, paste your RapidAPI key in the sidebar, upload a CSV with one domain per row (no header required), click **Validate API key**, then **Start traffic lookup**.

## Input format

A single-column CSV. Headers like `url`, `domain`, `website`, `сайт`, `домен`, `ссылка` are auto-detected and skipped. Anything else in the first row is treated as data.

```
example.com
https://www.cnn.com/
m.bbc.co.uk:443/news
```

All three rows above normalize to `example.com`, `cnn.com`, `bbc.co.uk`.

## Configuration knobs

Tunable constants at the top of `app.py`:

- `CONCURRENCY_LIMIT` — parallel requests cap (default 3, matches RapidAPI plan).
- `MAX_RETRIES` — total attempts per domain on transient errors.
- `BACKOFF_BASE` — exponential backoff base for retry delay.
- `CHECKPOINT_INTERVAL` — frequency of checkpoint UI announcements.
- `REQUEST_TIMEOUT` — per-request timeout in seconds.

## Resume after a crash

Results are persisted to `st.session_state["results"]` after every completed lookup. If a run is interrupted, hitting **Start traffic lookup** again re-runs only the domains that have not been resolved yet. To start from scratch, click **Reset checkpoint**.

## Security notes

The API key is never written to disk by this app and lives only in browser session memory. Do not share the running Streamlit URL while the key is loaded.

## License

MIT — see [LICENSE](LICENSE).

---

# DataLoom Traffic Checker (RU)

Массовая проверка трафика SimilarWeb через провайдера DataLoom (RapidAPI), обёрнутая в Streamlit-интерфейс. Разработано как часть pipeline для генерации лидов в ad-tech (Agent-1), где списки доменов фильтруются по проверкам `ads.txt` / `app-ads.txt` и порогам трафика перед обогащением контактов.

## Что нового в этой версии

| Область | Было | Стало |
|---------|------|-------|
| HTTP | Синхронные запросы, `time.sleep(0.35)` между вызовами | `aiohttp` + `asyncio.Semaphore(3)` — примерно в 3 раза быстрее, всё ещё в пределах лимита |
| Сбои | Одна попытка, сетевая ошибка → "-" | До 3 попыток с экспоненциальным backoff |
| Возобновление | Нет — закрытие вкладки теряет всё | Чекпоинты по каждому домену в `st.session_state`, повторные запуски идемпотентны |
| Проверка ключа | Невалидный ключ обнаруживается на первом домене | Предвалидация одним запросом к `google.com` |
| Расход квоты | Дубликаты URL опрашиваются дважды | Дедупликация входа после нормализации |
| Парсинг ответа | Рекурсивный поиск любого ключа со словом "visits" | Явный приоритетный список, предсказуемый результат |
| Очистка URL | Схема, www, путь | Добавлено: trailing-dot, порт, fragment, mobile-префикс |
| Вывод | Только полный список | Опциональный порог минимального трафика для скачиваемого CSV |

## Быстрый старт

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

Откройте напечатанный URL, вставьте ваш RapidAPI-ключ в боковом меню, загрузите CSV с одним доменом на строку (заголовок не обязателен), нажмите **Validate API key**, затем **Start traffic lookup**.

## Возобновление после сбоя

Результаты сохраняются в `st.session_state["results"]` после каждого успешного запроса. Если прогон прерван, повторное нажатие **Start traffic lookup** перезапросит только нерешённые домены. Чтобы начать с нуля — **Reset checkpoint**.

## Лицензия

MIT — см. [LICENSE](LICENSE).
