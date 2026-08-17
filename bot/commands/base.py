from aiogram import Router
from aiogram.types import Message
from aiogram.filters import CommandStart, Command
from bot.logger import logger
from editing.models import BotMessageForStart
from aiogram.types import FSInputFile
from bot.utils.bot_logging import log_command
from django.conf import settings

router = Router()


@router.message(CommandStart())
async def bot_start(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name or str(user_id)
    command = "/start"

    try:
        custom_message = await BotMessageForStart.objects.afirst()
        response_text = None

        if custom_message:
            response_text = custom_message.text
            if custom_message.image:
                file = FSInputFile(custom_message.image.path)
                await message.answer_photo(file, custom_message.text, parse_mode="HTML")
                response_text = "Сообщение с изображением"
            else:
                await message.answer(custom_message.text, parse_mode="HTML")
        else:
            pair = settings.PAIR or "не задана"
            response_text = f"Добро пожаловать в MexcBot! Пара этого бота: {pair}"
            await message.answer(response_text)

        await log_command(
            user_id=user_id,
            command=command,
            response=response_text,
            extra_data={
                "username": username,
                "chat_id": message.chat.id,
                "has_custom_message": custom_message is not None,
                "has_image": custom_message and custom_message.image is not None,
            },
        )

    except Exception as e:
        error_message = f"Error while processing {command} command: {e}"
        logger.error(error_message)

        fallback_response = "Добро пожаловать в MexcBot!"
        await message.answer(fallback_response)

        await log_command(
            user_id=user_id,
            command=command,
            response=fallback_response,
            success=False,
            extra_data={"username": username, "error": str(e)},
        )


@router.message(Command("help"))
async def bot_help(message: Message):
    pair = settings.PAIR or "не задана"
    text = (
        f"Скальпинг-бот MEXC. Пара этого экземпляра: <b>{pair}</b>\n\n"
        f"/set_keys — привязать API-ключи биржи\n"
        f"/parameters — профит, падение, пауза, сумма покупки\n"
        f"/autobuy — включить автоторговлю\n"
        f"/stop — выключить автоторговлю\n"
        f"/buy — одна ручная покупка + лимитная продажа\n"
        f"/price /balance /status /stats — рынок и сделки\n"
        f"/subscription — срок подписки\n\n"
        f"Чтобы торговать второй парой, запускается вторая копия бота "
        f"с другим TELEGRAM_TOKEN и другим PAIR в .env."
    )
    await message.answer(text, parse_mode="HTML")
    await log_command(
        user_id=message.from_user.id,
        command="/help",
        response=text,
        extra_data={"username": message.from_user.username},
    )


@router.message(Command("ping"))
async def bot_ping(message: Message):
    await message.answer("pong")
    await log_command(
        user_id=message.from_user.id,
        command="/ping",
        response="pong",
        extra_data={"username": message.from_user.username},
    )
