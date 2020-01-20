# Generated by Django 2.2.4 on 2020-01-18 08:22

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mentor', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='sessions',
            name='amount',
            field=models.DecimalField(decimal_places=4, default=1, max_digits=50),
        ),
        migrations.AddField(
            model_name='sessions',
            name='price_per_min',
            field=models.DecimalField(decimal_places=4, default=1, max_digits=50),
        ),
        migrations.AddField(
            model_name='sessions',
            name='tokenAddress',
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name='sessions',
            name='tokenName',
            field=models.CharField(default='ETH', max_length=255),
        ),
        migrations.AddField(
            model_name='sessions',
            name='tx_id',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='sessions',
            name='tx_received_on',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='sessions',
            name='tx_status',
            field=models.CharField(choices=[('na', 'na'), ('pending', 'pending'), ('success', 'success'), ('error', 'error'), ('unknown', 'unknown'), ('dropped', 'dropped')], db_index=True, default='na', max_length=9),
        ),
        migrations.AddField(
            model_name='sessions',
            name='tx_time',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]