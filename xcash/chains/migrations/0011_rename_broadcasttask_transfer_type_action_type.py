from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("chains", "0010_alter_broadcasttask_failure_reason"),
    ]

    operations = [
        migrations.RenameField(
            model_name="broadcasttask",
            old_name="transfer_type",
            new_name="action_type",
        ),
    ]
