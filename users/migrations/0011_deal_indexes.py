from django.db import migrations, models


class Migration(migrations.Migration):
    """Индексы под горячие выборки сделок.

    order_id ищется на каждом обновлении ордера по WebSocket и в reconciler'е
    раз в минуту по всем пользователям — без индекса это последовательное
    сканирование растущей таблицы.
    """

    dependencies = [
        ("users", "0010_deal_user_order_number"),
    ]

    operations = [
        migrations.AlterField(
            model_name="deal",
            name="order_id",
            field=models.CharField(db_index=True, max_length=64),
        ),
        migrations.AddIndex(
            model_name="deal",
            index=models.Index(
                fields=["user", "status", "is_autobuy"],
                name="users_deal_user_status_idx",
            ),
        ),
    ]
