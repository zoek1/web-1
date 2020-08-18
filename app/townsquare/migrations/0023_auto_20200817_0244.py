# Generated by Django 2.2.4 on 2020-08-17 02:44

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('grants', '0068_auto_20200708_0906'),
        ('townsquare', '0022_auto_20200528_1629'),
    ]

    operations = [
        migrations.AddField(
            model_name='favorite',
            name='grant',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, related_name='grant_favorites', to='grants.Grant'),
        ),
        migrations.AlterField(
            model_name='favorite',
            name='activity',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, to='dashboard.Activity'),
        ),
    ]