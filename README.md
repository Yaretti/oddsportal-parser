# OddsPortal Dropping Odds Parser

Експериментальний парсер сторінки:
https://www.oddsportal.com/dropping-odds/3/1/1/football/

## Що робить
- Відкриває сторінку через Playwright Chromium.
- Шукає блоки падіння коефіцієнтів у доступному HTML.
- Зберігає поточні результати в `data/oddsportal_latest.csv`.
- Додає нові/змінені значення в `data/oddsportal_history.csv`.
- Зберігає попередній стан у `data/oddsportal_state.json`.

## Важливо
Це початкова версія. OddsPortal може змінювати HTML, використовувати динамічне завантаження або обмежувати автоматизований доступ. У такому разі парсер потрібно адаптувати за фактичним HTML та логами GitHub Actions. Нуль знайдених записів завершує workflow помилкою, щоб не затирати результати порожнім файлом.

Поля CSV та їх коректність треба підтвердити першим запуском. Парсер не визначає фактичний грошовий обсяг ставок — лише рух коефіцієнтів.

## Встановлення локально
```bash
python -m pip install -r requirements.txt
playwright install chromium
python scrape_oddsportal.py
```

## Запуск на GitHub
Workflow `.github/workflows/oddsportal.yml` запускається вручну або за розкладом кожні 15 хвилин (GitHub Actions використовує UTC).

Для автоматичного запису CSV у репозиторій потрібні права `contents: write` у workflow та дозвіл GitHub Actions на запис у репозиторій.

## Зміна кількості сторінок
За замовчуванням парсер переглядає до 5 сторінок. Можна змінити змінну середовища `MAX_PAGES`.
