from django.db import migrations, models
import django.db.models.deletion


def stop_legacy_autobuy(apps, schema_editor):
    apps.get_model('users', 'User').objects.update(autobuy=False)


class Migration(migrations.Migration):
    dependencies = [('users', '0011_deal_indexes')]
    operations = [
        migrations.AddField(model_name='user', name='api_exchange', field=models.CharField(default='mexc', max_length=16)),
        migrations.AddField(model_name='user', name='execution_hold', field=models.TextField(blank=True, default='')),
        migrations.AddField(model_name='deal', name='exchange', field=models.CharField(default='mexc', max_length=16, db_index=True)),
        migrations.AddField(model_name='deal', name='migrated_from', field=models.OneToOneField(null=True, blank=True, on_delete=django.db.models.deletion.PROTECT, related_name='migrated_deal', to='users.deal')),
        migrations.CreateModel(name='OrderMigration', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('client_order_id', models.CharField(max_length=40, unique=True)),
            ('symbol', models.CharField(max_length=20)),
            ('quantity', models.DecimalField(max_digits=20, decimal_places=8)),
            ('price', models.DecimalField(max_digits=20, decimal_places=8)),
            ('state', models.CharField(default='PREPARED', max_length=20)),
            ('order_id', models.CharField(max_length=64, blank=True)),
            ('updated_at', models.DateTimeField(auto_now=True)),
            ('source', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, to='users.deal')),
        ]),
        migrations.RunPython(stop_legacy_autobuy, migrations.RunPython.noop),
    ]
