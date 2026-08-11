# Backfill RegionCorrection.source_ocr_request for recognition corrections that
# were accepted before migration 0024 introduced the permanent provenance FK.
#
# Before 0024 a recognition candidate's provenance lived only in the
# OcrRequest.accepted_correction shortcut. 0024 added source_ocr_request but
# (correctly) did not populate it for pre-existing rows. Without this backfill,
# provenance resolution (source_ocr_request != NULL => HTR/Vision) would report
# an already-accepted history candidate as Manual, letting the UI show both a
# Vision/HTR candidate and a Manual correction as "current" for the same text.
#
# For every OcrRequest that has an accepted_correction, point that correction's
# source_ocr_request at the producing request — unless it already has one
# (preserve anything already populated).

from django.db import migrations


def backfill_source_ocr_request(apps, schema_editor):
    OcrRequest = apps.get_model("workbench", "OcrRequest")
    updated = 0
    for req in OcrRequest.objects.exclude(accepted_correction_id__isnull=True).iterator():
        corr = req.accepted_correction
        if corr is not None and corr.source_ocr_request_id is None:
            corr.source_ocr_request = req
            corr.save(update_fields=["source_ocr_request"])
            updated += 1
    if updated:
        print(f"\nBackfilled source_ocr_request for {updated} correction(s)")


def noop(apps, schema_editor):
    # Rows backfilled here are also re-derived on next acceptance; reversing
    # would drop provenance for corrections that legitimately keep it.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("workbench", "0024_page_image_height_page_image_width_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_source_ocr_request, noop),
    ]
