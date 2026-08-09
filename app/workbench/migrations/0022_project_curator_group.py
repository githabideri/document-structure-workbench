from django.db import migrations

PROJECT_CURATOR_GROUP = "Project Curator"


def seed_curator_group(apps, schema_editor):
    """Create the Project Curator group and add every existing user to it.

    Existing users keep their ability to manage projects; newly created users
    afterwards are only added by an administrator (Django admin user/group or
    the manage_project_curator command).
    """
    Group = apps.get_model("auth", "Group")
    User = apps.get_model("auth", "User")
    group, _ = Group.objects.get_or_create(name=PROJECT_CURATOR_GROUP)
    users = list(User.objects.all())
    if users:
        group.user_set.add(*users)


def unseed_curator_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name=PROJECT_CURATOR_GROUP).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("workbench", "0021_userpreferences_must_change_password"),
    ]

    operations = [
        migrations.RunPython(seed_curator_group, unseed_curator_group),
    ]