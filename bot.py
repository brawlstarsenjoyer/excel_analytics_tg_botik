import os
import tempfile
import logging
from pathlib import Path
import pandas as pd
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# === НАСТРОЙКИ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ===
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не установлен в переменных окружения")

AUTHORIZED_IDS_STR = os.environ.get("AUTHORIZED_IDS", "")
AUTHORIZED_USER_IDS = set(map(int, AUTHORIZED_IDS_STR.split(","))) if AUTHORIZED_IDS_STR else set()

# Список приоритетных напитков
PRIORITY_DRINKS = {
    "Espresso",
    "Double espresso decaffeinated",
    "Chocolate Truffle",
    "Sakura Latte",
    "Matcha Latte",
    "Berry RAF",
    "Kakao Banana",
    "Masala Tea Latte",
    "Cheese & Orange Latte",
    "Double cappuccino vegan",
    "Flat White",
    "Flat White decaffeinated",
    "Flat white vegan",
    "Latte",
    "Latte decaffeinated",
    "Latte vegan",
    "Ice latte",
    "Ice latte decaffeinated",
    "Espresso decaffeinated",
    "Ice latte vegan",
    "Espresso tonic",
    "Espresso tonic decaffeinated",
    "Bumblebee",
    "Doppio(double espresso)",
    "Americano",
    "Americano decaffeinated",
    "Cappuccino",
    "Cappuccino decaffeinated",
    "Cacao",
    "Hot chocolate",
    "Cappuccino vegan",
    "Double Americano",
    "Double cappuccino"
}
PRIORITY_DRINKS_LOWER = {name.lower().strip() for name in PRIORITY_DRINKS}


def is_authorized(user_id: int) -> bool:
    """Проверка авторизации пользователя"""
    return not AUTHORIZED_USER_IDS or user_id in AUTHORIZED_USER_IDS


def analyze_excel(file_path: str) -> tuple[str, str, pd.DataFrame]:
    """Анализ Excel-файла с кассовым отчётом"""
    df_raw = pd.read_excel(file_path, header=None)

    # Поиск строки с заголовками
    header_row = None
    for i in range(len(df_raw)):
        if "Denumire marfa" in df_raw.iloc[i].values:
            header_row = i
            break
    
    if header_row is None:
        raise ValueError("❌ Не найдены заголовки. Убедитесь, что файл — отчёт кассы.")

    # Создание DataFrame с правильными заголовками
    df = df_raw.iloc[header_row:].copy()
    df.columns = df.iloc[0]
    df = df[1:].reset_index(drop=True)

    # Извлечение даты отчёта
    report_date = "неизвестна"
    if 'Data' in df.columns:
        non_empty = df['Data'].dropna()
        if not non_empty.empty:
            try:
                report_date = pd.to_datetime(non_empty.iloc[0], dayfirst=True).strftime('%d.%m.%Y')
            except Exception:
                report_date = str(non_empty.iloc[0]).strip()

    # Проверка необходимых столбцов
    required = ["Denumire marfa", "Cantitate", "Suma cu TVA fără reducere"]
    if not all(col in df.columns for col in required):
        raise ValueError(f"❌ Отсутствуют необходимые столбцы. Найдены: {list(df.columns)}")

    # Фильтрация и подготовка данных
    df = df[required].copy()
    df = df.dropna(subset=["Denumire marfa"])
    df = df[~df["Denumire marfa"].str.contains("Punga", na=False, case=False)]

    # Определение приоритетных напитков
    df['is_priority'] = df['Denumire marfa'].str.lower().str.strip().isin(PRIORITY_DRINKS_LOWER)

    # Группировка и агрегация
    result = df.groupby("Denumire marfa").agg(
        Количество=("Cantitate", "sum"),
        Сумма=("Suma cu TVA fără reducere", "sum"),
        is_priority=("is_priority", "any")
    ).round(2)

    # Сортировка: сначала приоритетные, затем по сумме
    result = result.sort_values(['is_priority', 'Сумма'], ascending=[False, False])
    result_for_save = result.drop(columns=['is_priority'])

    # Формирование текстового отчёта
    top_rows = result_for_save.head(30)
    text = f"📅 Дата отчёта: {report_date}\n\n📊 Отчёт по продажам:\n\n"
    text += top_rows.to_string()

    if len(result_for_save) > 30:
        text += f"\n\n... и ещё {len(result_for_save) - 30} позиций. Полный отчёт — в файле."

    return report_date, text, result_for_save


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("❌ У вас нет доступа к этому боту.")
        return
    
    await update.message.reply_text(
        "👋 Привет! Я бот для анализа кассовых отчётов.\n\n"
        "📎 Отправьте Excel-файл (.xlsx) с отчётом, и я:\n"
        "  • Проанализирую все продажи\n"
        "  • Сгруппирую по товарам\n"
        "  • Посчитаю количество и сумму\n"
        "  • Отсортирую по важности\n\n"
        "✨ Приоритетные напитки будут в начале списка!"
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик загруженных документов"""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("❌ У вас нет доступа.")
        return

    document = update.message.document
    file_name = (document.file_name or "").strip()
    mime_type = (document.mime_type or "").strip()

    # Проверяем по MIME-типу и расширению
    valid_mime = mime_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    valid_ext = file_name.lower().endswith(".xlsx")

    if not (valid_mime or valid_ext):
        await update.message.reply_text("❌ Пожалуйста, отправьте файл в формате .xlsx")
        return

    input_path = None
    output_path = None

    try:
        await update.message.reply_text("📥 Получаю файл...")

        # Скачивание файла
        file = await context.bot.get_file(document.file_id)
        with tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx') as tmp:
            await file.download_to_drive(tmp.name)
            input_path = tmp.name

        # Анализ файла
        await update.message.reply_text("⚙️ Обрабатываю данные...")
        report_date, text_report, df_result = analyze_excel(input_path)

        # Отправка текстового отчёта
        if len(text_report) < 4000:
            await update.message.reply_text(text_report)
        else:
            await update.message.reply_text(
                f"📅 Дата отчёта: {report_date}\n\n"
                "📊 Отчёт слишком длинный для отображения.\n"
                "Отправляю полный анализ в файле..."
            )

                # Формируем красивый текстовый отчёт
        txt_report = format_sales_report(report_date, df_result)

        # Отправляем текст, если он короткий
        if len(txt_report) < 4000:
            await update.message.reply_text(txt_report)
        else:
            # Иначе — отправляем как .txt файл
            output_filename = f"Отчёт_{report_date}.txt"
            output_path = os.path.join(tempfile.gettempdir(), output_filename)
            with open(output_path, 'w', encoding='utf-8') as f_out:
                f_out.write(txt_report)
            
            await update.message.reply_text("📤 Отправляю отчёт в формате .txt...")
            with open(output_path, 'rb') as f:
                await update.message.reply_document(
                    document=f,
                    filename=output_filename,
                    caption="✅ Отчёт готов!")

    except ValueError as e:
        logging.error(f"Ошибка валидации: {e}")
        await update.message.reply_text(f"❌ Ошибка в формате файла:\n{str(e)}")
    except Exception as e:
        logging.exception("Неожиданная ошибка при обработке файла")
        await update.message.reply_text(
            f"❌ Произошла ошибка при обработке:\n{str(e)[:500]}\n\n"
            "Убедитесь, что файл содержит корректный кассовый отчёт."
        )
    finally:
        # Очистка временных файлов
        if input_path and Path(input_path).exists():
            try:
                os.unlink(input_path)
            except Exception as e:
                logging.warning(f"Не удалось удалить временный файл {input_path}: {e}")
        
        if output_path and Path(output_path).exists():
            try:
                os.unlink(output_path)
            except Exception as e:
                logging.warning(f"Не удалось удалить выходной файл {output_path}: {e}")


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Глобальный обработчик ошибок"""
    logging.error(f"Exception while handling an update: {context.error}")
    
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "😔 Произошла непредвиденная ошибка. Попробуйте ещё раз или обратитесь к администратору."
            )
    except Exception:
        pass
def format_sales_report(report_date: str, df: pd.DataFrame) -> str:
    """Форматирует результат анализа в красивый текстовый отчёт, как в примере"""
    # Убедимся, что индекс — название товара
    df = df.copy()
    if df.index.name != "Denumire marfa":
        df.index.name = "Denumire marfa"
    
    items = df.reset_index()

    # Определяем кофейные напитки (по вашему списку)
    coffee_items = items[items["Denumire marfa"].str.lower().str.strip().isin(PRIORITY_DRINKS_LOWER)]
    other_items = items[~items["Denumire marfa"].str.lower().str.strip().isin(PRIORITY_DRINKS_LOWER)]

    total_revenue = items["Сумма"].sum()
    coffee_revenue = coffee_items["Сумма"].sum() if not coffee_items.empty else 0
    coffee_count = coffee_items["Количество"].sum() if not coffee_items.empty else 0

    # Состав кофейных напитков
    composition_parts = []
    for _, row in coffee_items.iterrows():
        name = row["Denumire marfa"].lower()
        # Приводим к виду как в примере: "americano", "cappuccino" и т.д.
        clean_name = name
        for full, short in [
            ("double espresso decaffeinated", "double espresso decaf"),
            ("flat white decaffeinated", "flat white decaf"),
            ("latte decaffeinated", "latte decaf"),
            ("ice latte decaffeinated", "ice latte decaf"),
            ("espresso decaffeinated", "espresso decaf"),
            ("americano decaffeinated", "americano decaf"),
            ("cappuccino decaffeinated", "cappuccino decaf"),
            ("doppio(double espresso)", "doppio"),
            ("double cappuccino vegan", "double cappuccino vegan"),
            ("flat white vegan", "flat white vegan"),
            ("latte vegan", "latte vegan"),
            ("ice latte vegan", "ice latte vegan"),
            ("cappuccino vegan", "cappuccino vegan"),
        ]:
            if clean_name == full:
                clean_name = short
                break
        # Убираем скобки и лишние символы
        clean_name = clean_name.replace("(", "").replace(")", "").replace("  ", " ").strip()
        composition_parts.append(f"{int(row['Количество'])} {clean_name}")

    composition = " + ".join(composition_parts) if composition_parts else ""

    # Формируем отчёт
    report = f"📅 Дата отчёта: {report_date}\n"
    report += f"💰 Общая выручка за день: {total_revenue:,.2f} лей\n".replace(",", " ")
    report += f"☕ Выручка от кофейных напитков: {coffee_revenue:,.2f} лей\n".replace(",", " ")
    report += f"🔢 Всего продано кофейных напитков: {int(coffee_count)} шт.\n"
    if composition:
        report += f"ℹ️  Состав: {composition}\n"
    other_revenue = total_revenue - coffee_revenue
    report += f"🍱 Выручка от остального: {other_revenue:,.2f} лей\n".replace(",", " ")
    report += "\n📊 Отчёт по продажам:\n"

    # Заголовок таблицы
    report += f"{'Denumire marfa':<40} {'Количество':<12} {'Сумма'}\n"
    report += "─" * 64 + "\n"

    # Все позиции (в том порядке, как в df — уже отсортированы)
    for _, row in items.iterrows():
        name = str(row["Denumire marfa"])
        qty = f"{row['Количество']:.2f}"
        amt = f"{row['Сумма']:.2f}"
        report += f"{name:<40} {qty:<12} {amt}\n"

    return report

def main():
    """Основная функция запуска бота"""
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    
    logger = logging.getLogger(__name__)
    logger.info("🚀 Запуск бота... v2025-10-27-1")

    # Создание приложения
    app = Application.builder().token(BOT_TOKEN).build()

        # Регистрация обработчиков
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    
    # Глобальный обработчик ошибок
    app.add_error_handler(error_handler)

    logger.info("✅ Бот запущен и ожидает сообщения...")
    
    # Запуск polling
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
