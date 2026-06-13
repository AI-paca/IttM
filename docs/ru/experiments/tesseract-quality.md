# Эксперимент качества Tesseract

[English](../../en/experiments/tesseract-quality.md) |
[Документация](../README.md)

## Правила

- Browser Tesseract.js, backend Tesseract и EasyOCR остаются независимыми.
- Этап обработки должен включаться отдельно через pipeline profile.
- При недостаточной уверенности сохраняется raw OCR text.
- Изменение не принимается, если теряет имена, цифры или реальные таблицы.

## Baseline

| Fixture                    | Backend Tesseract                           | EasyOCR CPU                             | Browser Tesseract.js                                 |
| -------------------------- | ------------------------------------------- | --------------------------------------- | ---------------------------------------------------- |
| `image (6).png`            | Ложная таблица из 570 ячеек, потеря ranking | Ложная таблица, часть имён потеряна     | Полезный raw text с местами 1-10 и score             |
| `photo_10...jpg`           | Читаемый slide                              | Потеря части строк                      | Node harness не воспроизводит полный DOM/Canvas путь |
| Длинный catalog screenshot | Шумный текст                                | Больше текста, но существенно медленнее | Шумный текст                                         |

## Подтверждение сетки по профилю

Коммит `11a0241` добавляет `grid_min_confirmed_cell_ratio`. Backend принимает
морфологически найденную таблицу только при достаточном числе замкнутых
прямоугольных ячеек. Standard backend profiles используют `0.35`.

На `image (6).png` ложная table reconstruction больше не уничтожает ranking:
Tesseract сохраняет title, места 1-10, имена и десять score. EasyOCR сохраняет
большинство имён и score как raw text, но порядок некоторых полей остаётся
неточным.

## Регрессии

- Slide и длинный catalog совпали с baseline по телу OCR.
- Curriculum PDF сохранил обнаружение реальных таблиц.
- Browser pipeline не изменялся.
- Backend, formatter и strict multilingual quality tests прошли.

Следующий этап качества должен быть отдельной веткой: row reconstruction с
проверкой полной rank sequence и score column, без удаления исходного текста.
