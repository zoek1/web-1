# Generated by Django 2.2.4 on 2020-04-13 12:23

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('grants', '0050_auto_20200329_2146'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='update',
            name='grant',
        ),
        migrations.DeleteModel(
            name='Milestone',
        ),
        migrations.DeleteModel(
            name='Update',
        ),
    ]
