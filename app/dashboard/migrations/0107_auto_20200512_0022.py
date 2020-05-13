# Generated by Django 2.2.4 on 2020-05-12 00:22

import django.contrib.postgres.fields.jsonb
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0106_auto_20200505_1824'),
    ]

    operations = [
        migrations.AlterField(
            model_name='bounty',
            name='raw_data',
            field=django.contrib.postgres.fields.jsonb.JSONField(blank=True),
        ),
        migrations.AlterField(
            model_name='earning',
            name='source_id',
            field=models.PositiveIntegerField(db_index=True),
        ),
    ]
