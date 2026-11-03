from django.contrib import admin
from users.models import User, Deal, OrderMigration


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ('telegram_id', 'name', 'created_at', 'api_exchange', 'pair', 'autobuy', 'buy_amount', 'profit', 'pause', 'loss', 'execution_hold')
    
@admin.register(Deal)
class DealAdmin(admin.ModelAdmin):
    list_filter = ('exchange', 'status')
    readonly_fields = ('migrated_from',)
    list_display = ('user', 'user_order_number', 'order_id', 'symbol', 'quantity', 'buy_price', 'sell_price', 'status', 'created_at', 'updated_at', 'is_autobuy')


@admin.register(OrderMigration)
class OrderMigrationAdmin(admin.ModelAdmin):
    list_display = ('source', 'client_order_id', 'symbol', 'quantity', 'price', 'state', 'order_id')
    readonly_fields = ('source', 'client_order_id', 'symbol', 'quantity', 'price', 'state', 'order_id', 'updated_at')

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
