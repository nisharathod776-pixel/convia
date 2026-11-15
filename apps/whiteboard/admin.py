from django.contrib import admin
from .models import WhiteboardOperation


@admin.register(WhiteboardOperation)
class WhiteboardOperationAdmin(admin.ModelAdmin):
    list_display = ("meeting", "op_type", "op_id", "user", "created_at")
    list_filter = ("op_type",)
