"""Seed processing presets (reversible)."""

from django.db import migrations


def seed_presets(apps, schema_editor):
    """Create default processing presets if they don't exist."""
    ProcessingPreset = apps.get_model("workbench", "ProcessingPreset")

    presets = [
        {
            "slug": "quick-extraction",
            "name": "Quick extraction",
            "description": (
                "Extract tables and text from a single PDF using the standard "
                "Docling pipeline. Generates page images, region detections, and "
                "table crops. Does not create review tasks."
            ),
            "description_short": "Extract tables and text — single model",
            "profile_a_enabled": True,
            "profile_b_enabled": False,
            "generate_crops": True,
            "create_review_tasks": False,
            "is_active": True,
            "sort_order": 10,
        },
        {
            "slug": "compare-table-methods",
            "name": "Compare table methods",
            "description": (
                "Extract tables using two different methods (Docling TableFormer "
                "and Granite Vision) and create blind review tasks so human "
                "reviewers can compare the results."
            ),
            "description_short": "Run two methods — create blind review tasks",
            "profile_a_enabled": True,
            "profile_b_enabled": True,
            "generate_crops": True,
            "create_review_tasks": True,
            "is_active": True,
            "sort_order": 20,
        },
        {
            "slug": "learn-with-examples",
            "name": "Learn with examples",
            "description": (
                "Benchmark/training mode — extract tables with full metadata "
                "including ground truth comparison. Use this when you have "
                "reference data and want to measure extraction quality."
            ),
            "description_short": "Benchmark mode with ground truth comparison",
            "profile_a_enabled": True,
            "profile_b_enabled": True,
            "generate_crops": True,
            "create_review_tasks": True,
            "is_active": True,
            "sort_order": 30,
        },
    ]

    for preset_data in presets:
        slug = preset_data.pop("slug")
        ProcessingPreset.objects.get_or_create(
            slug=slug,
            defaults=preset_data,
        )


def remove_presets(apps, schema_editor):
    """Remove seeded presets."""
    ProcessingPreset = apps.get_model("workbench", "ProcessingPreset")
    ProcessingPreset.objects.filter(
        slug__in=["quick-extraction", "compare-table-methods", "learn-with-examples"]
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("workbench", "0003_processingjob_preset_snapshot_projectmembership"),
    ]

    operations = [
        migrations.RunPython(seed_presets, remove_presets),
    ]
