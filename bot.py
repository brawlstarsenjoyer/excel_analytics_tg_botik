import os
import tempfile
import logging
from pathlib import Path
import pandas as pd
from telegram import Update, ReplyKeyboardMarkup
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


def analyze_excel(file_path: str) -> tuple[str, pd.DataFrame]:
    """Анализ Excel-файла с кассовым отчётом"""
    df_raw = pd.read_excel(file_path, header=None)

    header_row = None
    for i in range(len(df_raw)):
        if "Denumire marfa" in df_raw.iloc[i].values:
            header_row = i
            break

    if header_row is None:
        raise ValueError("❌ Не найдены заголовки. Убедитесь, что файл — отчёт кассы.")

    df = df_raw.iloc[header_row:].copy()
    df.columns = df.iloc[0]
    df = df[1:].reset_index(drop=True)

    report_date = "неизвестна"
    if 'Data' in df.columns:
        non_empty = df['Data'].dropna()
        if not non_empty.empty:
            try:
                report_date = pd.to_datetime(non_empty.iloc[0], dayfirst=True).strftime('%d.%m.%Y')
            except Exception:
                report_date = str(non_empty.iloc[0]).strip()

    required = ["Denumire marfa", "Cantitate", "Suma cu TVA fără reducere"]
    if not all(col in df.columns for col in required):
        raise ValueError(f"❌ Отсутствуют необходимые столбцы. Найдены: {list(df.columns)}")

    df = df[required].copy()
    df = df.dropna(subset=["Denumire marfa"])
    df = df[~df["Denumire marfa"].str.contains("Punga", na=False, case=False)]

    df['is_priority'] = df['Denumire marfa'].str.lower().str.strip().isin(PRIORITY_DRINKS_LOWER)

    result = df.groupby("Denumire marfa").agg(
        Количество=("Cantitate", "sum"),
        Сумма=("Suma cu TVA fără reducere", "sum"),
        is_priority=("is_priority", "any")
    ).round(2)

    result = result.sort_values(['is_priority', 'Сумма'], ascending=[False, False])
    result_for_save = result.drop(columns=['is_priority'])

    return report_date, result_for_save


def format_sales_report(report_date: str, df: pd.DataFrame) -> str:
    """Форматирует отчёт строго по шаблону из report (17)_для_telegram.txt"""
    df = df.copy()
    if df.index.name != "Denumire marfa":
        df.index.name = "Denumire marfa"
    items = df.reset_index()

    coffee_items = items[items["Denumire marfa"].str.lower().str.strip().isin(PRIORITY_DRINKS_LOWER)]
    total_revenue = items["Сумма"].sum()
    coffee_revenue = coffee_items["Сумма"].sum() if not coffee_items.empty else 0
    coffee_count = coffee_items["Количество"].sum() if not coffee_items.empty else 0

    name_map = {
        "Americano": "americano",
        "Cappuccino": "cappuccino",
        "Latte": "latte",
        "Berry RAF": "berry raf",
        "Double cappuccino": "double cappuccino",
        "Americano decaffeinated": "americano decaf",
        "Cheese & Orange Latte": "cheese & orange latte",
        "Sakura Latte": "sakura latte",
        "Latte decaffeinated": "latte decaf",
        "Cacao": "cacao",
        "Flat White": "flat white",
        "Flat White decaffeinated": "flat white decaf",
        "Espresso": "espresso",
        "Espresso decaffeinated": "espresso decaf",
        "Double Americano": "double americano",
        "Hot chocolate": "hot chocolate",
        "Chocolate Truffle": "chocolate truffle",
        "Doppio(double espresso)": "doppio(double espresso)",
        "Cappuccino vegan": "cappuccino vegan",
        "Double cappuccino vegan": "double cappuccino vegan",
        "Latte vegan": "latte vegan",
        "Ice latte": "ice latte",
        "Ice latte decaffeinated": "ice latte decaf",
        "Ice latte vegan": "ice latte vegan",
        "Espresso tonic": "espresso tonic",
        "Espresso tonic decaffeinated": "espresso tonic decaf",
        "Bumblebee": "bumblebee",
        "Masala Tea Latte": "masala tea latte",
        "Kakao Banana": "kakao banana",
        "Matcha Latte": "matcha latte",
    }

    composition_parts = []
    for _, row in coffee_items.iterrows():
        name = row["Denumire marfa"]
        clean = name_map.get(name, name.lower())
        qty = int(row["Количество"])
        composition_parts.append(f"{qty} {clean}")
    composition = " + ".join(composition_parts)

    lines = []
    lines.append(f"📅 Дата отчёта: {report_date}")
    lines.append(f"💰 Общая выручка за день: {total_revenue:,.2f} лей".replace(",", " "))
    lines.append(f"☕ Выручка от кофейных напитков: {coffee_revenue:,.2f} лей".replace(",", " "))
    lines.append(f"🔢 Всего продано кофейных напитков: {int(coffee_count)} шт.")
    if composition:
        lines.append(f"ℹ️  Состав: {composition}")
    lines.append(f"🍱 Выручка от остального: {(total_revenue - coffee_revenue):,.2f} лей".replace(",", " "))
    lines.append("")
    lines.append("📊 Отчёт по продажам:")
    lines.append("Denumire marfa                             Количество      Сумма")
    lines.append("────────────────────────────────────────────────────────────────")

    for _, row in items.iterrows():
        name = str(row["Denumire marfa"])
        qty = f"{row['Количество']:.2f}"
        amt = f"{row['Сумма']:.2f}"
        line = f"{name[:40]:<40} {qty:>12} {amt:>10}"
        lines.append(line)

    return "\n".join(lines)


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
        "  • Отсортирую по важности\n"
        "  • Отправлю красивый отчёт в формате .txt\n\n"
        "✨ Приоритетные напитки будут в начале списка!"
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик Excel-файлов"""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("❌ У вас нет доступа.")
        return

    document = update.message.document
    file_name = (document.file_name or "").strip().lower()
    mime_type = (document.mime_type or "").strip()

    if not (
        mime_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        or file_name.endswith(".xlsx")
    ):
        await update.message.reply_text("❌ Пожалуйста, отправьте файл в формате .xlsx")
        return

    input_path = None
    output_path = None

    try:
        await update.message.reply_text("📥 Получаю файл...")

        file = await context.bot.get_file(document.file_id)
        with tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx') as tmp:
            await file.download_to_drive(tmp.name)
            input_path = tmp.name

        await update.message.reply_text("⚙️ Обрабатываю данные...")
        report_date, df_result = analyze_excel(input_path)

        txt_content = format_sales_report(report_date, df_result)

        output_filename = f"Отчёт_{report_date.replace('.', '_')}.txt"
        output_path = os.path.join(tempfile.gettempdir(), output_filename)
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(txt_content)

        await update.message.reply_document(
            document=open(output_path, 'rb'),
            filename=output_filename,
            caption="✅ Отчёт готов!"
        )

    except Exception as e:
        logging.exception("Ошибка при обработке файла:")
        await update.message.reply_text(
            f"❌ Ошибка при обработке файла. Убедитесь, что это корректный кассовый отчёт."
        )
    finally:
        for path in [input_path, output_path]:
            if path and Path(path).exists():
                try:
                    os.unlink(path)
                except Exception as e:
                    logging.warning(f"Не удалось удалить файл {path}: {e}")


async def welcome_or_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает кнопку /start при любом текстовом сообщении"""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("❌ У вас нет доступа.")
        return

    keyboard = [["/start"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text(
        "👋 Нажмите кнопку ниже, чтобы начать:",
        reply_markup=reply_markup
    )


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.error(f"Exception while handling an update: {context.error}")
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "😔 Произошла ошибка. Попробуйте ещё раз."
            )
    except Exception:
        pass


def main():
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    logger = logging.getLogger(__name__)
    logger.info("🚀 Запуск бота...")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(
        filters.Document.mime_type("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet") |
        filters.Document.file_extension("xlsx"),
        handle_document
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, welcome_or_start))
    app.add_error_handler(error_handler)

    logger.info("✅ Бот запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
