from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("shop", "0006_order_shipped_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="region",
            field=models.CharField(max_length=20, null=True),
        ),
        migrations.AddIndex(
            model_name="order",
            index=models.Index(fields=["region"], name="order_region_idx"),
        ),
    ]
