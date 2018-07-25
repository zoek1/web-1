# Generated by Django 2.0.7 on 2018-07-17 01:51

from django.db import migrations
from dashboard.utils import get_web3
from django.conf import settings


def get_profile(Profile, username):
    profiles = Profile.objects.filter(handle__iexact=username)
    if profiles.exists():
        return profiles.first()
    return None


def save_address(profile, address):
    profile.preferred_payout_address = address
    profile.save()


def try_to_link_address_to_profile(Profile, username, address):
    if not username or not address:
        return False
    profile = get_profile(Profile, username)
    if profile:
        save_address(profile, address)
        #print('-')
        return True
    return False


def backwards_func(apps, schema_editor):
    pass


def forwards_func(apps, schema_editor):
    if settings.DEBUG:
        return
    try:
        Profile = apps.get_model('dashboard', 'Profile')
        print('bounty')
        Bounty = apps.get_model('dashboard', 'Bounty')
        for bounty in Bounty.objects.filter(network='mainnet', current_bounty=True):
            try_to_link_address_to_profile(Profile, bounty.bounty_owner_github_username, bounty.bounty_owner_address)
            for fulfillment in bounty.fulfillments.all():
                try_to_link_address_to_profile(Profile, fulfillment.fulfiller_address, fulfillment.fulfiller_address)
        print('fr')
        FaucetRequest = apps.get_model('faucet', 'FaucetRequest')
        for faucet in FaucetRequest.objects.all():
            try_to_link_address_to_profile(Profile, faucet.github_username, faucet.address)
        print('tip')
        Tip = apps.get_model('dashboard', 'Tip')
        for tip in Tip.objects.filter(network='mainnet').all():
            try:
                try_to_link_address_to_profile(Profile, tip.from_username, tip.from_address)
                w3 = get_web3(tip.network)
                tx = w3.eth.getTransaction(tip.receive_txid)
                if tx:
                    to = tx['to']
                    try_to_link_address_to_profile(Profile, tip.username, to)
            except:
                pass
        print('ens')
        ENSSubdomainRegistration = apps.get_model('enssubdomain', 'ENSSubdomainRegistration')
        for ens in ENSSubdomainRegistration.objects.all():
            if ens.profile:
                try_to_link_address_to_profile(Profile, ens.profile.handle, ens.subdomain_wallet_address)
    except Exception as e:
        print(e)


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0096_profile_preferred_payout_address'),
    ]

    operations = [
        migrations.RunPython(
            forwards_func, backwards_func,
        ),
    ]
