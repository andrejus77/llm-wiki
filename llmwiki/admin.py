"""Django admin (/admin): a free edit UI for rules, tags, pages, sections and the job/call logs. Read-mostly."""
from django.contrib import admin

from llmwiki import models as m


@admin.register(m.SkipRule)
class SkipRuleAdmin(admin.ModelAdmin):
    list_display = ("kind", "sender", "subject", "recipient", "template", "action", "folder", "enabled", "hits", "why")
    list_filter = ("kind", "enabled", "action")
    search_fields = ("sender", "subject", "template")


@admin.register(m.Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("type", "value", "state", "threads", "last_seen", "why")
    list_filter = ("type", "state")
    search_fields = ("value",)


class SectionInline(admin.TabularInline):
    model = m.Section
    extra = 0
    fields = ("key", "version", "text")


@admin.register(m.Page)
class PageAdmin(admin.ModelAdmin):
    list_display = ("title", "level", "status", "updated_at", "archived")
    list_filter = ("level", "status", "archived")
    search_fields = ("title", "slug")
    inlines = [SectionInline]


@admin.register(m.Email)
class EmailAdmin(admin.ModelAdmin):
    list_display = ("day", "from_addr", "subject", "status", "importance", "thread")
    list_filter = ("status", "importance", "kind", "lang")
    search_fields = ("subject", "from_addr", "message_id", "note")
    readonly_fields = ("message_id", "thread", "parent", "dup_of")


@admin.register(m.Job)
class JobAdmin(admin.ModelAdmin):
    list_display = ("stage", "unit", "status", "attempts", "created_at")
    list_filter = ("stage", "status")
    search_fields = ("unit", "key")


@admin.register(m.LlmCall)
class LlmCallAdmin(admin.ModelAdmin):
    list_display = ("created_at", "stage", "unit", "endpoint", "status", "round", "latency_ms", "prompt_tokens")
    list_filter = ("stage", "status", "endpoint")
    search_fields = ("unit",)


for model in (m.Thread, m.Item, m.OpenIssue, m.SectionHistory, m.Run, m.Event, m.ChatMessage, m.SettingsVersion):
    admin.site.register(model)
