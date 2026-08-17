import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from editing.models import BotMessageForStart
from parameters.models import BaseParameters


class Command(BaseCommand):
    help = "Create default trading parameters, start message and admin user if missing"

    def handle(self, *args, **options):
        if not BaseParameters.objects.exists():
            BaseParameters.objects.create(
                profit=0.5,
                pause=10,
                loss=0.5,
                buy_amount=10.00,
            )
            self.stdout.write(self.style.SUCCESS("Created default BaseParameters"))
        else:
            self.stdout.write("BaseParameters already exist")

        if not BotMessageForStart.objects.exists():
            pair = os.getenv("PAIR") or "не задана"
            BotMessageForStart.objects.create(
                text=(
                    f"Добро пожаловать в MexcBot.\n\n"
                    f"Этот экземпляр торгует пару: <b>{pair}</b>\n\n"
                    f"1. /set_keys — API-ключи MEXC\n"
                    f"2. Дождитесь активации подписки в админке\n"
                    f"3. /parameters — профит, падение, пауза, сумма\n"
                    f"4. /autobuy — запуск автоторговли"
                )
            )
            self.stdout.write(self.style.SUCCESS("Created default /start message"))

        User = get_user_model()
        username = os.getenv("DJANGO_SUPERUSER_USERNAME", "admin")
        password = os.getenv("DJANGO_SUPERUSER_PASSWORD")
        email = os.getenv("DJANGO_SUPERUSER_EMAIL", "admin@localhost")
        if not password:
            self.stdout.write("DJANGO_SUPERUSER_PASSWORD is not set, skip admin user")
            return
        if User.objects.filter(username=username).exists():
            self.stdout.write(f"Superuser '{username}' already exists")
            return
        User.objects.create_superuser(username=username, email=email, password=password)
        self.stdout.write(self.style.SUCCESS(f"Created superuser '{username}'"))
