# -*- coding: utf-8 -*-
'''
    Copyright (C) 2020 Gitcoin Core

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as published
    by the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with this program. If not, see <http://www.gnu.org/licenses/>.

'''
from __future__ import print_function, unicode_literals

import hashlib
import json
import logging
import os
import time
from copy import deepcopy
from datetime import datetime
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Avg, Count, Prefetch, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.template import loader
from django.template.response import TemplateResponse
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape, strip_tags
from django.utils.http import is_safe_url
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

import magic
from app.utils import clean_str, ellipses, get_default_network
from avatar.models import AvatarTheme
from avatar.utils import get_avatar_context_for_user
from avatar.views_3d import avatar3dids_helper
from bleach import clean
from bounty_requests.models import BountyRequest
from cacheops import invalidate_obj
from chat.tasks import (
    add_to_channel, associate_chat_to_profile, chat_notify_default_props, create_channel_if_not_exists,
)
from dashboard.context import quickstart as qs
from dashboard.utils import (
    ProfileHiddenException, ProfileNotFoundException, get_bounty_from_invite_url, get_orgs_perms, profile_helper,
)
from economy.utils import ConversionRateNotFoundError, convert_amount, convert_token_to_usdt
from eth_utils import to_checksum_address, to_normalized_address
from gas.utils import recommend_min_gas_price_to_confirm_in_time
from git.utils import (
    get_auth_url, get_gh_issue_details, get_github_user_data, get_url_dict, is_github_token_valid, search_users,
)
from kudos.models import KudosTransfer, Token, Wallet
from kudos.utils import humanize_name
from mailchimp3 import MailChimp
from marketing.mails import admin_contact_funder, bounty_uninterested
from marketing.mails import funder_payout_reminder as funder_payout_reminder_mail
from marketing.mails import (
    new_reserved_issue, share_bounty, start_work_approved, start_work_new_applicant, start_work_rejected,
    wall_post_email,
)
from marketing.models import EmailSubscriber, Keyword
from oauth2_provider.decorators import protected_resource
from pytz import UTC
from ratelimit.decorators import ratelimit
from rest_framework.renderers import JSONRenderer
from retail.helpers import get_ip
from retail.utils import programming_languages, programming_languages_full
from townsquare.models import Comment
from townsquare.views import get_following_tribes, get_tags
from web3 import HTTPProvider, Web3

from .export import (
    ActivityExportSerializer, BountyExportSerializer, CustomAvatarExportSerializer, GrantExportSerializer,
    ProfileExportSerializer, filtered_list_data,
)
from .helpers import (
    bounty_activity_event_adapter, get_bounty_data_for_activity, handle_bounty_views, load_files_in_directory,
)
from .models import (
    Activity, Answer, BlockedURLFilter, Bounty, BountyEvent, BountyFulfillment, BountyInvites, CoinRedemption,
    CoinRedemptionRequest, Coupon, Earning, FeedbackEntry, HackathonEvent, HackathonProject, HackathonRegistration,
    HackathonSponsor, Interest, LabsResearch, Option, Poll, PortfolioItem, Profile, ProfileSerializer, ProfileView,
    Question, SearchHistory, Sponsor, Subscription, Tool, ToolVote, TribeMember, UserAction, UserVerificationModel,
)
from .notifications import (
    maybe_market_tip_to_email, maybe_market_tip_to_github, maybe_market_tip_to_slack, maybe_market_to_email,
    maybe_market_to_github, maybe_market_to_slack, maybe_market_to_user_slack,
)
from .router import HackathonEventSerializer, HackathonProjectSerializer, TribesSerializer, TribesTeamSerializer
from .utils import (
    apply_new_bounty_deadline, get_bounty, get_bounty_id, get_context, get_custom_avatars, get_unrated_bounties_count,
    get_web3, has_tx_mined, is_valid_eth_address, re_market_bounty, record_user_action_on_interest,
    release_bounty_to_the_public, sync_payout, web3_process_bounty,
)

logger = logging.getLogger(__name__)

confirm_time_minutes_target = 4

# web3.py instance
w3 = Web3(HTTPProvider(settings.WEB3_HTTP_PROVIDER))


@protected_resource()
def oauth_connect(request, *args, **kwargs):
    active_user_profile = Profile.objects.filter(user_id=request.user.id).select_related()[0]
    from marketing.utils import should_suppress_notification_email
    user_profile = {
        "login": active_user_profile.handle,
        "email": active_user_profile.user.email,
        "name": active_user_profile.user.get_full_name(),
        "handle": active_user_profile.handle,
        "id": f'{active_user_profile.user.id}',
        "auth_data": f'{active_user_profile.user.id}',
        "auth_service": "gitcoin",
        "notify_props": chat_notify_default_props(active_user_profile)
    }
    return JsonResponse(user_profile, status=200, safe=False)


def org_perms(request):
    if request.user.is_authenticated and getattr(request.user, 'profile', None):
        profile = request.user.profile
        response_data = get_orgs_perms(profile)
    else:
        return JsonResponse(
            {'error': _('You must be authenticated via github to use this feature!')},
             status=401)
    return JsonResponse({'orgs': response_data}, safe=False)


def record_user_action(user, event_name, instance):
    instance_class = instance.__class__.__name__.lower()
    kwargs = {
        'action': event_name,
        'metadata': {f'{instance_class}_pk': instance.pk},
    }

    if isinstance(user, User):
        kwargs['user'] = user
    elif isinstance(user, str):
        try:
            user = User.objects.get(username=user)
            kwargs['user'] = user
        except User.DoesNotExist:
            return

    if hasattr(user, 'profile'):
        kwargs['profile'] = user.profile

    try:
        UserAction.objects.create(**kwargs)
    except Exception as e:
        # TODO: sync_profile?
        logger.error(f"error in record_action: {e} - {event_name} - {instance}")


def record_bounty_activity(bounty, user, event_name, interest=None):
    """Creates Activity object.

    Args:
        bounty (dashboard.models.Bounty): Bounty
        user (string): User name
        event_name (string): Event name
        interest (dashboard.models.Interest): Interest

    Raises:
        None

    Returns:
        None
    """
    kwargs = {
        'activity_type': event_name,
        'bounty': bounty,
        'metadata': get_bounty_data_for_activity(bounty)
    }
    if isinstance(user, str):
        try:
            user = User.objects.get(username=user)
        except User.DoesNotExist:
            return

    if hasattr(user, 'profile'):
        kwargs['profile'] = user.profile
    else:
        return

    if event_name == 'worker_applied':
        kwargs['metadata']['approve_worker_url'] = bounty.approve_worker_url(user.profile)
        kwargs['metadata']['reject_worker_url'] = bounty.reject_worker_url(user.profile)
    elif event_name in ['worker_approved', 'worker_rejected'] and interest:
        kwargs['metadata']['worker_handle'] = interest.profile.handle

    try:
        if event_name in bounty_activity_event_adapter:
            event = BountyEvent.objects.create(bounty=bounty,
                event_type=bounty_activity_event_adapter[event_name],
                created_by=kwargs['profile'])
            bounty.handle_event(event)
        activity = Activity.objects.create(**kwargs)

        # leave a comment on townsquare IFF someone left a start work plan
        if event_name in ['start_work', 'worker_applied'] and interest and interest.issue_message:
            from townsquare.models import Comment
            Comment.objects.create(
                profile=interest.profile,
                activity=activity,
                comment=interest.issue_message,
                )

    except Exception as e:
        logger.error(f"error in record_bounty_activity: {e} - {event_name} - {bounty} - {user}")


def helper_handle_access_token(request, access_token):
    # https://gist.github.com/owocki/614a18fbfec7a5ed87c97d37de70b110
    # interest API via token
    github_user_data = get_github_user_data(access_token)
    request.session['handle'] = github_user_data['login']
    profile = Profile.objects.filter(handle=request.session['handle'].lower()).first()
    request.session['profile_id'] = profile.pk


def create_new_interest_helper(bounty, user, issue_message):
    approval_required = bounty.permission_type == 'approval'
    acceptance_date = timezone.now() if not approval_required else None
    profile_id = user.profile.pk
    interest = Interest.objects.create(
        profile_id=profile_id,
        issue_message=issue_message,
        pending=approval_required,
        acceptance_date=acceptance_date
    )
    record_bounty_activity(bounty, user, 'start_work' if not approval_required else 'worker_applied', interest=interest)
    bounty.interested.add(interest)
    record_user_action(user, 'start_work', interest)
    maybe_market_to_slack(bounty, 'start_work' if not approval_required else 'worker_applied')
    maybe_market_to_user_slack(bounty, 'start_work' if not approval_required else 'worker_applied')
    return interest


@csrf_exempt
def gh_login(request):
    """Attempt to redirect the user to Github for authentication."""
    return redirect('social:begin', backend='github')


@csrf_exempt
def gh_org_login(request):
    """Attempt to redirect the user to Github for authentication."""
    return redirect('social:begin', backend='gh-custom')


def get_interest_modal(request):
    bounty_id = request.GET.get('pk')
    if not bounty_id:
        raise Http404

    try:
        bounty = Bounty.objects.get(pk=bounty_id)
    except Bounty.DoesNotExist:
        raise Http404

    if bounty.event and request.user.is_authenticated:
        is_registered = HackathonRegistration.objects.filter(registrant=request.user.profile, hackathon_id=bounty.event.id) or None
    else:
        is_registered = None

    context = {
        'bounty': bounty,
        'active': 'get_interest_modal',
        'title': _('Add Interest'),
        'user_logged_in': request.user.is_authenticated,
        'is_registered': is_registered,
        'login_link': '/login/github?next=' + request.GET.get('redirect', '/')
    }
    return TemplateResponse(request, 'addinterest.html', context)


@csrf_exempt
@require_POST
def new_interest(request, bounty_id):
    """Claim Work for a Bounty.

    :request method: POST

    Args:
        bounty_id (int): ID of the Bounty.

    Returns:
        dict: The success key with a boolean value and accompanying error.

    """
    profile_id = request.user.profile.pk if request.user.is_authenticated and hasattr(request.user, 'profile') else None

    access_token = request.GET.get('token')
    if access_token:
        helper_handle_access_token(request, access_token)
        github_user_data = get_github_user_data(access_token)
        profile = Profile.objects.prefetch_related('bounty_set') \
            .filter(handle=github_user_data['login'].lower()).first()
        profile_id = profile.pk
    else:
        profile = request.user.profile if profile_id else None

    if not profile_id:
        return JsonResponse(
            {'error': _('You must be authenticated via github to use this feature!')},
            status=401)

    try:
        bounty = Bounty.objects.get(pk=bounty_id)
    except Bounty.DoesNotExist:
        raise Http404

    if bounty.is_project_type_fulfilled:
        return JsonResponse({
            'error': _(f'There is already someone working on this bounty.'),
            'success': False},
            status=401)

    max_num_issues = profile.max_num_issues_start_work
    currently_working_on = profile.active_bounties.count()

    if currently_working_on >= max_num_issues:
        return JsonResponse(
            {
                'error': _(f'You cannot work on more than {max_num_issues} issues at once'),
                'success': False
            },
            status=401
        )

    if profile.no_times_slashed_by_staff():
        return JsonResponse({
            'error': _('Because a staff member has had to remove you from a bounty in the past, you are unable to start'
                       'more work at this time. Please leave a message on slack if you feel this message is in error.'),
            'success': False},
            status=401)

    try:
        Interest.objects.get(profile_id=profile_id, bounty=bounty)
        return JsonResponse({
            'error': _('You have already started work on this bounty!'),
            'success': False},
            status=401)
    except Interest.DoesNotExist:
        issue_message = request.POST.get("issue_message")
        interest = create_new_interest_helper(bounty, request.user, issue_message)
        if interest.pending:
            start_work_new_applicant(interest, bounty)

    except Interest.MultipleObjectsReturned:
        bounty_ids = bounty.interested \
            .filter(profile_id=profile_id) \
            .values_list('id', flat=True) \
            .order_by('-created')[1:]

        Interest.objects.filter(pk__in=list(bounty_ids)).delete()

        return JsonResponse({
            'error': _('You have already started work on this bounty!'),
            'success': False},
            status=401)

    msg = _("You have started work.")
    approval_required = bounty.permission_type == 'approval'
    if approval_required:
        msg = _("You have applied to start work. If approved, you will be notified via email.")
    elif not approval_required and not bounty.bounty_reserved_for_user:
        msg = _("You have started work.")
    elif not approval_required and bounty.bounty_reserved_for_user != profile:
        msg = _("You have applied to start work, but the bounty is reserved for another user.")
        JsonResponse({
            'error': msg,
            'success': False},
            status=401)
    return JsonResponse({
        'success': True,
        'profile': ProfileSerializer(interest.profile).data,
        'msg': msg,
        'status': 200
    })


@csrf_exempt
@require_POST
def post_comment(request):
    profile_id = request.user.profile if request.user.is_authenticated and hasattr(request.user, 'profile') else None
    if profile_id is None:
        return JsonResponse({
            'success': False,
            'msg': '',
        })

    bounty_id = request.POST.get('bounty_id')
    bountyObj = Bounty.objects.get(pk=bounty_id)
    receiver_profile = Profile.objects.filter(handle=request.POST.get('review[receiver]').lower()).first()
    fbAmount = FeedbackEntry.objects.filter(
        sender_profile=profile_id,
        receiver_profile=receiver_profile,
        bounty=bountyObj
    ).count()
    if fbAmount > 0:
        return JsonResponse({
            'success': False,
            'msg': 'There is already an approval comment',
        })

    kwargs = {
        'bounty': bountyObj,
        'sender_profile': profile_id,
        'receiver_profile': receiver_profile,
        'rating': request.POST.get('review[rating]', '0'),
        'satisfaction_rating': request.POST.get('review[satisfaction_rating]', '0'),
        'private': not bool(request.POST.get('review[public]', '0') == "1"),
        'communication_rating': request.POST.get('review[communication_rating]', '0'),
        'speed_rating': request.POST.get('review[speed_rating]', '0'),
        'code_quality_rating': request.POST.get('review[code_quality_rating]', '0'),
        'recommendation_rating': request.POST.get('review[recommendation_rating]', '0'),
        'comment': request.POST.get('review[comment]', 'No comment.'),
        'feedbackType': request.POST.get('review[reviewType]','approver')
    }

    feedback = FeedbackEntry.objects.create(**kwargs)
    feedback.save()
    return JsonResponse({
            'success': True,
            'msg': 'Finished.'
        })

def rating_modal(request, bounty_id, username):
    # TODO: will be changed to the new share
    """Rating modal.

    Args:
        pk (int): The primary key of the bounty to be rated.

    Raises:
        Http404: The exception is raised if no associated Bounty is found.

    Returns:
        TemplateResponse: The rate bounty view.

    """
    try:
        bounty = Bounty.objects.get(pk=bounty_id)
    except Bounty.DoesNotExist:
        return JsonResponse({'errors': ['Bounty doesn\'t exist!']},
                            status=401)

    params = get_context(
        ref_object=bounty,
    )
    params['receiver']=username
    params['user'] = request.user if request.user.is_authenticated else None

    return TemplateResponse(request, 'rating_modal.html', params)


def rating_capture(request):
    # TODO: will be changed to the new share
    """Rating capture.

    Args:
        pk (int): The primary key of the bounty to be rated.

    Raises:
        Http404: The exception is raised if no associated Bounty is found.

    Returns:
        TemplateResponse: The rate bounty capture modal.

    """
    user = request.user if request.user.is_authenticated else None
    if not user:
        return JsonResponse(
            {'error': _('You must be authenticated via github to use this feature!')},
            status=401)

    return TemplateResponse(request, 'rating_capture.html')


def unrated_bounties(request):
    """Rating capture.

    Args:
        pk (int): The primary key of the bounty to be rated.

    Raises:
        Http404: The exception is raised if no associated Bounty is found.

    Returns:
        TemplateResponse: The rate bounty capture modal.

    """
    # request.user.profile if request.user.is_authenticated and getattr(request.user, 'profile', None) else None
    unrated_count = 0
    user = request.user.profile if request.user.is_authenticated else None
    if not user:
        return JsonResponse(
            {'error': _('You must be authenticated via github to use this feature!')},
            status=401)

    if user:
        unrated_count = get_unrated_bounties_count(user)

    # data = json.dumps(unrated)
    return JsonResponse({
        'unrated': unrated_count,
    }, status=200)


@csrf_exempt
@require_POST
def remove_interest(request, bounty_id):
    """Unclaim work from the Bounty.

    Can only be called by someone who has started work

    :request method: POST

    post_id (int): ID of the Bounty.

    Returns:
        dict: The success key with a boolean value and accompanying error.

    """
    profile_id = request.user.profile.pk if request.user.is_authenticated and getattr(request.user, 'profile', None) else None

    access_token = request.GET.get('token')
    if access_token:
        helper_handle_access_token(request, access_token)
        github_user_data = get_github_user_data(access_token)
        profile = Profile.objects.filter(handle=github_user_data['login'].lower()).first()
        profile_id = profile.pk

    if not profile_id:
        return JsonResponse(
            {'error': _('You must be authenticated via github to use this feature!')},
            status=401)

    try:
        bounty = Bounty.objects.get(pk=bounty_id)
    except Bounty.DoesNotExist:
        return JsonResponse({'errors': ['Bounty doesn\'t exist!']},
                            status=401)

    try:
        interest = Interest.objects.get(profile_id=profile_id, bounty=bounty)
        record_user_action(request.user, 'stop_work', interest)
        record_bounty_activity(bounty, request.user, 'stop_work')
        bounty.interested.remove(interest)
        interest.delete()
        maybe_market_to_slack(bounty, 'stop_work')
        maybe_market_to_user_slack(bounty, 'stop_work')
    except Interest.DoesNotExist:
        return JsonResponse({
            'errors': [_('You haven\'t expressed interest on this bounty.')],
            'success': False},
            status=401)
    except Interest.MultipleObjectsReturned:
        interest_ids = bounty.interested \
            .filter(
                profile_id=profile_id,
                bounty=bounty
            ).values_list('id', flat=True) \
            .order_by('-created')

        bounty.interested.remove(*interest_ids)
        Interest.objects.filter(pk__in=list(interest_ids)).delete()

    return JsonResponse({
        'success': True,
        'msg': _("You've stopped working on this, thanks for letting us know."),
        'status': 200
    })


@csrf_exempt
@require_POST
def extend_expiration(request, bounty_id):
    """Extend expiration of the Bounty.

    Can only be called by funder or staff of the bounty.

    :request method: POST

    post_id (int): ID of the Bounty.

    Returns:
        dict: The success key with a boolean value and accompanying error.

    """
    user = request.user if request.user.is_authenticated else None

    if not user:
        return JsonResponse(
            {'error': _('You must be authenticated via github to use this feature!')},
            status=401)

    try:
        bounty = Bounty.objects.get(pk=bounty_id)
    except Bounty.DoesNotExist:
        return JsonResponse({'errors': ['Bounty doesn\'t exist!']},
                            status=401)

    is_funder = bounty.is_funder(user.username.lower()) if user else False
    if is_funder:
        deadline = round(int(request.POST.get('deadline')))
        result = apply_new_bounty_deadline(bounty, deadline)
        record_user_action(request.user, 'extend_expiration', bounty)
        record_bounty_activity(bounty, request.user, 'extend_expiration')

        return JsonResponse({
            'success': True,
            'msg': _(result['msg']),
        })

    return JsonResponse({
        'error': _("You must be funder to extend expiration"),
    }, status=200)


@csrf_exempt
@require_POST
def cancel_reason(request):
    """Add Cancellation Reason for Bounty during Cancellation

    request method: POST

    Params:
        pk (int): ID of the Bounty.
        canceled_bounty_reason (string): STRING with cancel  reason
    """
    user = request.user if request.user.is_authenticated else None

    if not user:
        return JsonResponse(
            {'error': _('You must be authenticated via github to use this feature!')},
            status=401
        )

    try:
        bounty = Bounty.objects.get(pk=request.POST.get('pk'))
    except Bounty.DoesNotExist:
        return JsonResponse(
            {'errors': ['Bounty not found']},
            status=404
        )

    is_funder = bounty.is_funder(user.username.lower()) if user else False
    if is_funder:
        canceled_bounty_reason = request.POST.get('canceled_bounty_reason', '')
        bounty.canceled_bounty_reason = canceled_bounty_reason
        bounty.save()

        return JsonResponse({
            'success': True,
            'msg': _("cancel reason added."),
        })

    return JsonResponse(
        {
            'error': _('bounty cancellation is bounty funder operatio'),
        },
        status=410
    )


@require_POST
@csrf_exempt
def uninterested(request, bounty_id, profile_id):
    """Remove party from given bounty

    Can only be called by the bounty funder

    :request method: GET

    Args:
        bounty_id (int): ID of the Bounty
        profile_id (int): ID of the interested profile

    Params:
        slashed (str): if the user will be slashed or not

    Returns:
        dict: The success key with a boolean value and accompanying error.
    """
    try:
        bounty = Bounty.objects.get(pk=bounty_id)
    except Bounty.DoesNotExist:
        return JsonResponse({'errors': ['Bounty doesn\'t exist!']},
                            status=401)
    is_logged_in = request.user.is_authenticated
    is_funder = bounty.is_funder(request.user.username.lower())
    is_staff = request.user.is_staff
    is_moderator = request.user.profile.is_moderator if hasattr(request.user, 'profile') else False
    if not is_logged_in or (not is_funder and not is_staff and not is_moderator):
        return JsonResponse(
            {'error': 'Only bounty funders are allowed to remove users!'},
            status=401)

    slashed = request.POST.get('slashed')
    interest = None
    try:
        interest = Interest.objects.get(profile_id=profile_id, bounty=bounty)
        bounty.interested.remove(interest)
        maybe_market_to_slack(bounty, 'stop_work')
        maybe_market_to_user_slack(bounty, 'stop_work')
        if is_staff or is_moderator:
            event_name = "bounty_removed_slashed_by_staff" if slashed else "bounty_removed_by_staff"
        else:
            event_name = "bounty_removed_by_funder"
        record_user_action_on_interest(interest, event_name, None)
        record_bounty_activity(bounty, interest.profile.user, 'stop_work')
        interest.delete()
    except Interest.DoesNotExist:
        return JsonResponse({
            'errors': ['Party haven\'t expressed interest on this bounty.'],
            'success': False},
            status=401)
    except Interest.MultipleObjectsReturned:
        interest_ids = bounty.interested \
            .filter(
                profile_id=profile_id,
                bounty=bounty
            ).values_list('id', flat=True) \
            .order_by('-created')

        bounty.interested.remove(*interest_ids)
        Interest.objects.filter(pk__in=list(interest_ids)).delete()

    profile = Profile.objects.get(id=profile_id)
    if profile.user and profile.user.email and interest:
        bounty_uninterested(profile.user.email, bounty, interest)
    else:
        print("no email sent -- user was not found")

    return JsonResponse({
        'success': True,
        'msg': _("You've stopped working on this, thanks for letting us know."),
    })


def onboard_avatar(request):
    return redirect('/onboard/contributor?steps=avatar')

def onboard(request, flow=None):
    """Handle displaying the first time user experience flow."""
    if flow not in ['funder', 'contributor', 'profile']:
        if not request.user.is_authenticated:
            raise Http404
        target = 'funder' if request.user.profile.persona_is_funder else 'contributor'
        new_url = f'/onboard/{target}'
        return redirect(new_url)
    elif flow == 'funder':
        onboard_steps = ['github', 'metamask', 'avatar']
    elif flow == 'contributor':
        onboard_steps = ['github', 'metamask', 'avatar', 'skills', 'job']
    elif flow == 'profile':
        onboard_steps = ['avatar']

    profile = None
    if request.user.is_authenticated and getattr(request.user, 'profile', None):
        profile = request.user.profile

    steps = []
    if request.GET:
        steps = request.GET.get('steps', [])
        if steps:
            steps = steps.split(',')

    if (steps and 'github' not in steps) or 'github' not in onboard_steps:
        if not request.user.is_authenticated or request.user.is_authenticated and not getattr(
            request.user, 'profile', None
        ):
            login_redirect = redirect('/login/github?next=' + request.get_full_path())
            return login_redirect

    if request.POST.get('eth_address') and request.user.is_authenticated and getattr(request.user, 'profile', None):
        profile = request.user.profile
        eth_address = request.GET.get('eth_address')
        valid_address = is_valid_eth_address(eth_address)
        if valid_address:
            profile.preferred_payout_address = eth_address
            profile.save()
        return JsonResponse({'OK': True})

    theme = request.GET.get('theme', 'unisex')
    from avatar.views_3d import get_avatar_attrs
    skin_tones = get_avatar_attrs(theme, 'skin_tones')
    hair_tones = get_avatar_attrs(theme, 'hair_tones')
    background_tones = get_avatar_attrs(theme, 'background_tones')

    params = {
        'title': _('Onboarding Flow'),
        'steps': steps or onboard_steps,
        'flow': flow,
        'profile': profile,
        'theme': theme,
        'avatar_options': AvatarTheme.objects.filter(active=True).order_by('-popularity'),
        '3d_avatar_params': None if 'avatar' not in steps else avatar3dids_helper(theme),
        'possible_background_tones': background_tones,
        'possible_skin_tones': skin_tones,
        'possible_hair_tones': hair_tones,
    }
    params.update(get_avatar_context_for_user(request.user))
    return TemplateResponse(request, 'ftux/onboard.html', params)


@login_required
def users_directory(request):
    """Handle displaying users directory page."""
    from retail.utils import programming_languages, programming_languages_full

    keywords = programming_languages + programming_languages_full

    params = {
        'is_staff': request.user.is_staff,
        'avatar_url': request.build_absolute_uri(static('v2/images/twitter_cards/tw_cards-07.png')) ,
        'active': 'users',
        'title': 'Users',
        'meta_title': "",
        'meta_description': "",
        'keywords': keywords
    }

    if request.path == '/tribes/explore':
        params['explore'] = 'explore_tribes'

    return TemplateResponse(request, 'dashboard/users.html', params)


def users_fetch_filters(profile_list, skills, bounties_completed, leaderboard_rank, rating, organisation, hackathon_id = ""):
    if not settings.DEBUG:
        network = 'mainnet'
    else:
        network = 'rinkeby'

    if skills:
        profile_list = profile_list.filter(keywords__icontains=skills)

    if len(bounties_completed) == 2:
        profile_list = profile_list.annotate(
            count=Count('fulfilled')
        ).filter(
                count__gte=bounties_completed[0],
                count__lte=bounties_completed[1],
            )

    if len(leaderboard_rank) == 2:
        profile_list = profile_list.filter(
            leaderboard_ranks__isnull=False,
            leaderboard_ranks__leaderboard='quarterly_earners',
            leaderboard_ranks__rank__gte=leaderboard_rank[0],
            leaderboard_ranks__rank__lte=leaderboard_rank[1],
            leaderboard_ranks__active=True,
        )

    if rating != 0:
        profile_list = profile_list.filter(average_rating__gte=rating)

    if organisation:
        profile_list1 = profile_list.filter(
            fulfilled__bounty__network=network,
            fulfilled__accepted=True,
            fulfilled__bounty__github_url__icontains=organisation
        )
        profile_list2 = profile_list.filter(
            organizations__contains=[organisation.lower()]
        )
        profile_list = (profile_list1 | profile_list2).distinct()
    if hackathon_id:
        profile_list = profile_list.filter(
            hackathon_registration__hackathon=hackathon_id
        )
    return profile_list



@require_GET
def projects_fetch(request):
    q = clean(request.GET.get('search', ''), strip=True)
    order_by = clean(request.GET.get('order_by', '-created_on'), strip=True)
    skills = clean(request.GET.get('skills', ''), strip=True)
    filters = clean(request.GET.get('filters', ''), strip=True)
    sponsor = clean(request.GET.get('sponsor', ''), strip=True)
    # TODO: refactor for pagination
    page = clean(request.GET.get('page', 1))
    hackathon_id = clean(request.GET.get('hackathon', ''))

    if hackathon_id:
        try:
            hackathon_event = HackathonEvent.objects.get(id=hackathon_id)
        except HackathonEvent.DoesNotExist:
            hackathon_event = HackathonEvent.objects.last()

        projects = HackathonProject.objects.filter(hackathon=hackathon_event).exclude(
            status='invalid').prefetch_related('profiles', 'bounty').order_by(order_by, 'id')

        if sponsor:
            projects = projects.filter(
                Q(bounty__github_url__icontains=sponsor)
            )
    elif sponsor:
        sponsor_profile = Profile.objects.get(handle__iexact=sponsor)
        if sponsor_profile:
            projects = HackathonProject.objects.filter(Q(hackathon__sponsor_profiles__in=[sponsor_profile]) | Q(bounty__bounty_owner_github_username__in=[sponsor])).exclude(
                status='invalid').prefetch_related('profiles', 'bounty').order_by(order_by, 'id')
        else:
            projects = []
    if q:
        projects = projects.filter(
            Q(name__icontains=q) |
            Q(summary__icontains=q) |
            Q(profiles__handle__icontains=q)
        )

    if skills:
        projects = projects.filter(
            Q(profiles__keywords__icontains=skills)
        )

    if filters == 'winners':
        projects = projects.filter(
            Q(badge__isnull=False)
        )

    if filters == 'lfm':
        projects = projects.filter(
            Q(looking_members=True)
        )

    projects_data = HackathonProjectSerializer(projects.distinct('created_on', 'id').all(), many=True)

    params = {
        'data': projects_data.data,
        'has_next': False,
        'count': projects.all().count(),
        'num_pages': 1
    }

    return JsonResponse(params, status=200, safe=False)

@require_GET
def users_fetch(request):
    """Handle displaying users."""
    q = request.GET.get('search', '')
    skills = request.GET.get('skills', '')
    persona = request.GET.get('persona', '')
    limit = int(request.GET.get('limit', 20))
    page = int(request.GET.get('page', 1))
    default_sort = '-actions_count' if persona != 'tribe' else '-follower_count'
    order_by = request.GET.get('order_by', default_sort)
    bounties_completed = request.GET.get('bounties_completed', '').strip().split(',')
    leaderboard_rank = request.GET.get('leaderboard_rank', '').strip().split(',')
    rating = int(request.GET.get('rating', '0'))
    organisation = request.GET.get('organisation', '')
    tribe = request.GET.get('tribe', '')
    hackathon_id = request.GET.get('hackathon', '')
    user_filter = request.GET.get('user_filter', '')

    user_id = request.GET.get('user', None)
    if user_id:
        current_user = User.objects.get(id=int(user_id))
    else:
        current_user = request.user if hasattr(request, 'user') and request.user.is_authenticated else None

    if not current_user:
        return redirect('/login/github?next=' + request.get_full_path())
    current_profile = Profile.objects.get(user=current_user)

    if not settings.DEBUG:
        network = 'mainnet'
    else:
        network = 'rinkeby'
    if current_user:
        profile_list = Profile.objects.prefetch_related(
                'fulfilled', 'leaderboard_ranks', 'feedbacks_got'
            ).exclude(hide_profile=True)
    else:
        profile_list = Profile.objects.prefetch_related(
                'fulfilled', 'leaderboard_ranks', 'feedbacks_got'
            ).exclude(hide_profile=True)

    if q:
        profile_list = profile_list.filter(Q(follower__org__handle__in=[tribe]) | Q(organizations_fk__handle__in=[tribe])).distinct()

    if tribe:

        profile_list = profile_list.filter(Q(follower__org__handle__in=[tribe]) | Q(organizations_fk__handle__in=[tribe])).distinct()

        if user_filter and user_filter != 'all':
            if user_filter == 'owners':
                profile_list = profile_list.filter(Q(organizations_fk__handle__in=[tribe]))
            elif user_filter == 'members':
                profile_list = profile_list.exclude(Q(organizations_fk__handle__in=[tribe]))
            elif user_filter == 'hackers':
                profile_list = profile_list.filter(Q(fulfilled__bounty__github_url__icontains=tribe) | Q(project_profiles__hackathon__sponsor_profiles__handle__in=[tribe]) | Q(hackathon_registration__hackathon__sponsor_profiles__handle__in=[tribe]))


    show_banner = None
    if persona:
        if persona == 'funder':
            profile_list = profile_list.filter(dominant_persona='funder')
        if persona == 'coder':
            profile_list = profile_list.filter(dominant_persona='hunter')
        if persona == 'tribe':
            profile_list = profile_list.filter(data__type='Organization')
            show_banner = static('v2/images/tribes/logo-with-text.svg')

    profile_list = users_fetch_filters(
        profile_list,
        skills,
        bounties_completed,
        leaderboard_rank,
        rating,
        organisation,
        hackathon_id
    )

    def previous_worked():
        if current_user.profile.persona_is_funder:
            return Count(
                'fulfilled',
                filter=Q(
                    fulfilled__bounty__network=network,
                    fulfilled__accepted=True,
                    fulfilled__bounty__bounty_owner_github_username__iexact=current_user.profile.handle
                )
            )

        return Count(
            'bounties_funded__fulfillments',
            filter=Q(
                bounties_funded__fulfillments__bounty__network=network,
                bounties_funded__fulfillments__accepted=True,
                bounties_funded__fulfillments__fulfiller_github_username=current_user.profile.handle
            )
        )

    if request.GET.get('type') == 'explore_tribes':
        profile_list = Profile.objects.filter(is_org=True).order_by('-follower_count', 'id')

        if q:
            profile_list = profile_list.filter(Q(handle__icontains=q) | Q(keywords__icontains=q))

        all_pages = Paginator(profile_list, limit)
        this_page = all_pages.page(page)

        this_page = Profile.objects_full.filter(pk__in=[ele.pk for ele in this_page]).order_by('-follower_count', 'id')

    else:
        try:
            profile_list = profile_list.order_by(order_by, '-earnings_count', 'id')
        except profile_list.FieldError:
            profile_list = profile_list.order_by('-earnings_count', 'id')

        profile_list = profile_list.values_list('pk', flat=True)
        all_pages = Paginator(profile_list, limit)
        this_page = all_pages.page(page)

        profile_list = Profile.objects_full.filter(pk__in=[ele for ele in this_page]).order_by('-earnings_count', 'id').exclude(handle__iexact='gitcoinbot')

        this_page = profile_list

    all_users = []
    params = dict()

    for user in this_page:
        if not user:
            continue
        followers = TribeMember.objects.filter(org=user)

        is_following = True if followers.filter(profile=current_profile).count() else False
        profile_json = {
            k: getattr(user, k) for k in
            ['id', 'actions_count', 'created_on', 'handle', 'hide_profile',
            'show_job_status', 'job_location', 'job_salary', 'job_search_status',
            'job_type', 'linkedin_url', 'resume', 'remote', 'keywords',
            'organizations', 'is_org', 'last_chat_status', 'chat_id']}

        profile_json['is_following'] = is_following

        follower_count = followers.count()
        profile_json['follower_count'] = follower_count

        if user.is_org:
            profile_dict = user.__dict__
            profile_json['count_bounties_on_repo'] = profile_dict.get('as_dict').get('count_bounties_on_repo')
            sum_eth = profile_dict.get('as_dict').get('sum_eth_on_repos')
            profile_json['sum_eth_on_repos'] = round(sum_eth if sum_eth is not None else 0, 2)
            profile_json['tribe_description'] = user.tribe_description
            profile_json['rank_org'] = user.rank_org
        else:
            count_work_completed = user.get_fulfilled_bounties(network=network).count()

            profile_json['job_status'] = user.job_status_verbose if user.job_search_status else None
            profile_json['previously_worked'] = False # user.previous_worked_count > 0
            profile_json['position_contributor'] = user.get_contributor_leaderboard_index()
            profile_json['position_funder'] = user.get_funder_leaderboard_index()
            profile_json['work_done'] = count_work_completed
            profile_json['verification'] = user.get_my_verified_check
            profile_json['avg_rating'] = user.get_average_star_rating()

            if not user.show_job_status:
                for key in ['job_salary', 'job_location', 'job_type',
                            'linkedin_url', 'resume', 'job_search_status',
                            'remote', 'job_status']:
                    del profile_json[key]

        if user.avatar_baseavatar_related.exists():
            user_avatar = user.avatar_baseavatar_related.first()
            profile_json['avatar_id'] = user_avatar.pk
            profile_json['avatar_url'] = user_avatar.avatar_url
        if user.data:
            user_data = user.data
            profile_json['blog'] = user_data['blog']

        all_users.append(profile_json)

    # dumping and loading the json here quickly passes serialization issues - definitely can be a better solution
    params['data'] = json.loads(json.dumps(all_users, default=str))
    params['has_next'] = all_pages.page(page).has_next()
    params['count'] = all_pages.count
    params['num_pages'] = all_pages.num_pages
    params['show_banner'] = show_banner
    params['persona'] = persona

    # log this search, it might be useful for matching purposes down the line

    # TODO, move this to the worker
    try:
        SearchHistory.objects.update_or_create(
            search_type='users',
            user=request.user,
            data=request.GET,
            ip_address=get_ip(request)
        )
    except Exception as e:
        logger.debug(e)
        pass

    return JsonResponse(params, status=200, safe=False)


def get_user_bounties(request):
    """Get user open bounties.

    Args:
        request (int): get user by id or use authenticated.

    Variables:

    Returns:
        json: array of bounties.

    """
    user_id = request.GET.get('user', None)
    if user_id:
        profile = Profile.objects.get(id=int(user_id))
    else:
        profile = request.user.profile if request.user.is_authenticated and hasattr(request.user, 'profile') else None
    if not settings.DEBUG:
        network = 'mainnet'
    else:
        network = 'rinkeby'

    params = dict()
    results = []
    all_bounties = Bounty.objects.current().filter(bounty_owner_github_username__iexact=profile.handle, network=network)

    if len(all_bounties) > 0:
        is_funder = True
    else:
        is_funder = False

    open_bounties = all_bounties.exclude(idx_status='cancelled').exclude(idx_status='done')
    for bounty in open_bounties:
        bounty_json = {}
        bounty_json = bounty.to_standard_dict()
        bounty_json['url'] = bounty.url

        results.append(bounty_json)
    params['data'] = json.loads(json.dumps(results, default=str))
    params['is_funder'] = is_funder
    return JsonResponse(params, status=200, safe=False)


def dashboard(request):
    """Handle displaying the dashboard."""

    keyword = request.GET.get('keywords', False)
    title = keyword.title() + str(_(" Bounties ")) if keyword else str(_('Issue Explorer'))
    params = {
        'active': 'dashboard',
        'title': title,
        'avatar_url': request.build_absolute_uri(static('v2/images/twitter_cards/tw_cards-01 copy.png')),
        'meta_title': "Issue & Open Bug Bounty Marketplace | Gitcoin",
        'meta_description': "Find open bug bounties & freelance development jobs including crypto bounty reward value in USD, expiration date and bounty age.",
        'keywords': json.dumps([str(key) for key in Keyword.objects.all().values_list('keyword', flat=True)]),
    }
    return TemplateResponse(request, 'dashboard/index.html', params)


def ethhack(request):
    """Handle displaying ethhack landing page."""
    from dashboard.context.hackathon import eth_hack

    params = eth_hack

    return TemplateResponse(request, 'dashboard/hackathon/index.html', params)


def beyond_blocks_2019(request):
    """Handle displaying ethhack landing page."""
    from dashboard.context.hackathon import beyond_blocks_2019

    params = beyond_blocks_2019
    params['card_desc'] = params['meta_description']

    return TemplateResponse(request, 'dashboard/hackathon/index.html', params)


def accept_bounty(request):
    """Process the bounty.

    Args:
        pk (int): The primary key of the bounty to be accepted.

    Raises:
        Http404: The exception is raised if no associated Bounty is found.

    Returns:
        TemplateResponse: The accept bounty view.

    """
    bounty = handle_bounty_views(request)
    params = get_context(
        ref_object=bounty,
        user=request.user if request.user.is_authenticated else None,
        confirm_time_minutes_target=confirm_time_minutes_target,
        active='accept_bounty',
        title=_('Process Issue'),
    )
    params['open_fulfillments'] = bounty.fulfillments.filter(accepted=False)
    return TemplateResponse(request, 'process_bounty.html', params)


def invoice(request):
    """invoice view.

    Args:
        pk (int): The primary key of the bounty to be accepted.

    Raises:
        Http404: The exception is raised if no associated Bounty is found.

    Returns:
        TemplateResponse: The invoice  view.

    """
    bounty = handle_bounty_views(request)

    # only allow invoice viewing if admin or iff bounty funder
    is_funder = bounty.is_funder(request.user.username)
    is_staff = request.user.is_staff
    has_view_privs = is_funder or is_staff
    if not has_view_privs:
        raise Http404

    params = get_context(
        ref_object=bounty,
        user=request.user if request.user.is_authenticated else None,
        confirm_time_minutes_target=confirm_time_minutes_target,
        active='invoice_view',
        title=_('Invoice'),
    )
    params['accepted_fulfillments'] = bounty.fulfillments.filter(accepted=True)
    params['tips'] = [
        tip for tip in bounty.tips.send_happy_path() if ((tip.username == request.user.username and tip.username) or (tip.from_username == request.user.username and tip.from_username) or request.user.is_staff)
    ]
    params['total'] = bounty._val_usd_db if params['accepted_fulfillments'] else 0
    for tip in params['tips']:
        if tip.value_in_usdt:
            params['total'] += Decimal(tip.value_in_usdt)

    if bounty.fee_amount > 0:
        params['fee_value_in_usdt'] = bounty.fee_amount * Decimal(bounty.get_value_in_usdt) / bounty.value_true
        params['total'] = params['total'] + params['fee_value_in_usdt']
    return TemplateResponse(request, 'bounty/invoice.html', params)


def social_contribution_modal(request):
    # TODO: will be changed to the new share
    """Social Contributuion to the bounty.

    Args:
        pk (int): The primary key of the bounty to be accepted.

    Raises:
        Http404: The exception is raised if no associated Bounty is found.

    Returns:
        TemplateResponse: The accept bounty view.

    """
    from .utils import get_bounty_invite_url
    bounty = handle_bounty_views(request)

    params = get_context(
        ref_object=bounty,
        user=request.user if request.user.is_authenticated else None,
        confirm_time_minutes_target=confirm_time_minutes_target,
        active='social_contribute',
        title=_('Social Contribute'),
    )
    params['invite_url'] = f'{settings.BASE_URL}issue/{get_bounty_invite_url(request.user.username, bounty.pk)}'
    promo_text = str(_("Check out this bounty that pays out ")) + f"{bounty.get_value_true} {bounty.token_name} {params['invite_url']}"
    for keyword in bounty.keywords_list:
        promo_text += f" #{keyword}"
    params['promo_text'] = promo_text
    return TemplateResponse(request, 'social_contribution_modal.html', params)


@csrf_exempt
@require_POST
def bulk_invite(request):
    """Invite users with matching skills to a bounty.

    Args:
        bounty_id (int): The primary key of the bounty to be accepted.
        skills (string): Comma separated list of matching keywords.

    Raises:
        Http403: The exception is raised if the user is not authenticated or
                 the args are missing.
        Http401: The exception is raised if the user is not a staff member.

    Returns:
        Http200: Json response with {'status': 200, 'msg': 'email_sent'}.

    """
    from .utils import get_bounty_invite_url

    if not request.user.is_staff:
        return JsonResponse({'status': 401,
                             'msg': 'Unauthorized'})

    inviter = request.user if request.user.is_authenticated else None
    skills = ','.join(request.POST.getlist('params[skills][]', []))
    bounties_completed = request.POST.get('params[bounties_completed]', '').strip().split(',')
    leaderboard_rank = request.POST.get('params[leaderboard_rank]', '').strip().split(',')
    rating = int(request.POST.get('params[rating]', '0'))
    organisation = request.POST.get('params[organisation]', '')
    bounty_id = request.POST.get('bountyId')

    if None in (bounty_id, inviter):
        return JsonResponse({'success': False}, status=400)

    bounty = Bounty.objects.current().get(id=int(bounty_id))

    profiles = Profile.objects.prefetch_related(
                'fulfilled', 'leaderboard_ranks', 'feedbacks_got'
            ).exclude(hide_profile=True)

    profiles = users_fetch_filters(
        profiles,
        skills,
        bounties_completed,
        leaderboard_rank,
        rating,
        organisation)

    invite_url = f'{settings.BASE_URL}issue/{get_bounty_invite_url(request.user.username, bounty_id)}'

    if len(profiles):
        for profile in profiles:
            try:
                bounty_invite = BountyInvites.objects.create(
                    status='pending'
                )
                bounty_invite.bounty.add(bounty)
                bounty_invite.inviter.add(inviter)
                bounty_invite.invitee.add(profile.user)
                msg = request.POST.get('msg', '')
                share_bounty([profile.email], msg, inviter.profile, invite_url, False)
            except Exception as e:
                logging.exception(e)
    else:
        return JsonResponse({'success': False}, status=403)
    return JsonResponse({'status': 200,
                         'msg': 'email_sent'})


@csrf_exempt
@require_POST
def social_contribution_email(request):
    """Social Contribution Email

    Returns:
        JsonResponse: Success in sending email.
    """
    from .utils import get_bounty_invite_url

    emails = []
    bounty_id = request.POST.get('bountyId')
    user_ids = request.POST.getlist('usersId[]', [])
    invite_url = f'{settings.BASE_URL}issue/{get_bounty_invite_url(request.user.username, bounty_id)}'

    inviter = request.user if request.user.is_authenticated else None
    bounty = Bounty.objects.current().get(id=int(bounty_id))
    for user_id in user_ids:
        profile = Profile.objects.get(id=int(user_id))
        bounty_invite = BountyInvites.objects.create(
            status='pending'
        )
        bounty_invite.bounty.add(bounty)
        bounty_invite.inviter.add(inviter)
        bounty_invite.invitee.add(profile.user)
        emails.append(profile.email)

    msg = request.POST.get('msg', '')

    try:
        share_bounty(emails, msg, request.user.profile, invite_url, True)
        response = {
            'status': 200,
            'msg': 'email_sent',
        }
    except Exception as e:
        logging.exception(e)
        response = {
            'status': 500,
            'msg': 'Email not sent',
        }
    return JsonResponse(response)


@login_required
def payout_bounty(request):
    """Payout the bounty.

    Args:
        pk (int): The primary key of the bounty to be accepted.

    Raises:
        Http404: The exception is raised if no associated Bounty is found.

    Returns:
        TemplateResponse: The accept bounty view.

    """
    bounty = handle_bounty_views(request)

    params = get_context(
        ref_object=bounty,
        user=request.user if request.user.is_authenticated else None,
        confirm_time_minutes_target=confirm_time_minutes_target,
        active='payout_bounty',
        title=_('Payout'),
    )
    return TemplateResponse(request, 'payout_bounty.html', params)


@login_required
def bulk_payout_bounty(request):
    """Payout the bounty.

    Args:
        pk (int): The primary key of the bounty to be accepted.

    Raises:
        Http404: The exception is raised if no associated Bounty is found.

    Returns:
        TemplateResponse: The accept bounty view.

    """
    bounty = handle_bounty_views(request)

    params = get_context(
        ref_object=bounty,
        user=request.user if request.user.is_authenticated else None,
        confirm_time_minutes_target=confirm_time_minutes_target,
        active='payout_bounty',
        title=_('Advanced Payout'),
    )
    params['open_fulfillments'] = bounty.fulfillments.filter(accepted=False)
    return TemplateResponse(request, 'bulk_payout_bounty.html', params)


@require_GET
@login_required
def fulfill_bounty(request):
    """Fulfill a bounty.

    Parameters:
        pk (int): The primary key of the Bounty.
        standard_bounties_id (int): The standard bounties ID of the Bounty.
        network (str): The network of the Bounty.
        githubUsername (str): The Github Username of the referenced user.

    Raises:
        Http404: The exception is raised if no associated Bounty is found.

    Returns:
        TemplateResponse: The fulfill bounty view.

    """
    bounty = handle_bounty_views(request)
    if not bounty.has_started_work(request.user.username):
        raise PermissionDenied
    params = get_context(
        ref_object=bounty,
        github_username=request.GET.get('githubUsername'),
        user=request.user if request.user.is_authenticated else None,
        confirm_time_minutes_target=confirm_time_minutes_target,
        active='fulfill_bounty',
        title=_('Submit Work')
    )
    params['is_bounties_network'] = bounty.is_bounties_network
    return TemplateResponse(request, 'bounty/fulfill.html', params)


@login_required
def increase_bounty(request):
    """Increase a bounty as the funder.

    Args:
        pk (int): The primary key of the bounty to be increased.

    Raises:
        Http404: The exception is raised if no associated Bounty is found.

    Returns:
        TemplateResponse: The increase bounty view.

    """
    bounty = handle_bounty_views(request)
    user = request.user if request.user.is_authenticated else None
    is_funder = bounty.is_funder(user.username.lower()) if user else False

    params = get_context(
        ref_object=bounty,
        user=user,
        confirm_time_minutes_target=confirm_time_minutes_target,
        active='increase_bounty',
        title=_('Increase Bounty'),
    )

    params['is_funder'] = json.dumps(is_funder)
    params['FEE_PERCENTAGE'] = request.user.profile.fee_percentage if request.user.is_authenticated else 10

    coupon_code = request.GET.get('coupon', False)
    if coupon_code:
        coupon = Coupon.objects.get(code=coupon_code)
        if coupon.expiry_date > datetime.now().date():
            params['FEE_PERCENTAGE'] = coupon.fee_percentage
            params['coupon_code'] = coupon.code
        else:
            params['expired_coupon'] = True

    return TemplateResponse(request, 'bounty/increase.html', params)


def cancel_bounty(request):
    """Kill an expired bounty.

    Args:
        pk (int): The primary key of the bounty to be cancelled.

    Raises:
        Http404: The exception is raised if no associated Bounty is found.

    Returns:
        TemplateResponse: The cancel bounty view.

    """
    bounty = handle_bounty_views(request)
    params = get_context(
        ref_object=bounty,
        user=request.user if request.user.is_authenticated else None,
        confirm_time_minutes_target=confirm_time_minutes_target,
        active='kill_bounty',
        title=_('Cancel Bounty')
    )

    params['is_bounties_network'] = bounty.is_bounties_network

    return TemplateResponse(request, 'bounty/cancel.html', params)


def helper_handle_admin_override_and_hide(request, bounty):
    admin_override_and_hide = request.GET.get('admin_override_and_hide', False)
    if admin_override_and_hide:
        is_moderator = request.user.profile.is_moderator if hasattr(request.user, 'profile') else False
        if getattr(request.user, 'profile', None) and is_moderator or request.user.is_staff:
            bounty.admin_override_and_hide = True
            bounty.save()
            messages.success(request, _('Bounty is now hidden'))
        else:
            messages.warning(request, _('Only moderators may do this.'))


def helper_handle_admin_contact_funder(request, bounty):
    admin_contact_funder_txt = request.GET.get('admin_contact_funder', False)
    if admin_contact_funder_txt:
        is_staff = request.user.is_staff
        is_moderator = request.user.profile.is_moderator if hasattr(request.user, 'profile') else False
        if is_staff or is_moderator:
            # contact funder
            admin_contact_funder(bounty, admin_contact_funder_txt, request.user)
            messages.success(request, _(f'Bounty message has been sent'))
        else:
            messages.warning(request, _('Only moderators or the funder of this bounty may do this.'))


def helper_handle_mark_as_remarket_ready(request, bounty):
    admin_mark_as_remarket_ready = request.GET.get('admin_toggle_as_remarket_ready', False)
    if admin_mark_as_remarket_ready:
        is_staff = request.user.is_staff
        is_moderator = request.user.profile.is_moderator if hasattr(request.user, 'profile') else False
        if is_staff or is_moderator:
            bounty.admin_mark_as_remarket_ready = not bounty.admin_mark_as_remarket_ready
            bounty.save()
            if bounty.admin_mark_as_remarket_ready:
                messages.success(request, _(f'Bounty is now remarket ready'))
            else:
                messages.success(request, _(f'Bounty is now NOT remarket ready'))
        else:
            messages.warning(request, _('Only moderators or the funder of this bounty may do this.'))


def helper_handle_suspend_auto_approval(request, bounty):
    suspend_auto_approval = request.GET.get('suspend_auto_approval', False)
    if suspend_auto_approval:
        is_staff = request.user.is_staff
        is_moderator = request.user.profile.is_moderator if hasattr(request.user, 'profile') else False
        if is_staff or is_moderator:
            bounty.admin_override_suspend_auto_approval = True
            bounty.save()
            messages.success(request, _(f'Bounty auto approvals are now suspended'))
        else:
            messages.warning(request, _('Only moderators or the funder of this bounty may do this.'))


def helper_handle_override_status(request, bounty):
    admin_override_satatus = request.GET.get('admin_override_satatus', False)
    if admin_override_satatus:
        is_staff = request.user.is_staff
        if is_staff:
            valid_statuses = [ele[0] for ele in Bounty.STATUS_CHOICES]
            valid_statuses = valid_statuses + [""]
            valid_statuses_str = ",".join(valid_statuses)
            if admin_override_satatus not in valid_statuses:
                messages.warning(request, str(
                    _('Not a valid status choice.  Please choose a valid status (no quotes): ')) + valid_statuses_str)
            else:
                bounty.override_status = admin_override_satatus
                bounty.save()
                messages.success(request, _(f'Status updated to "{admin_override_satatus}" '))
        else:
            messages.warning(request, _('Only staff or the funder of this bounty may do this.'))


def helper_handle_snooze(request, bounty):
    snooze_days = int(request.GET.get('snooze', 0))
    if snooze_days:
        is_funder = bounty.is_funder(request.user.username.lower())
        is_staff = request.user.is_staff
        is_moderator = request.user.profile.is_moderator if hasattr(request.user, 'profile') else False
        if is_funder or is_staff or is_moderator:
            bounty.snooze_warnings_for_days = snooze_days
            bounty.save()
            messages.success(request, _(f'Warning messages have been snoozed for {snooze_days} days'))
        else:
            messages.warning(request, _('Only moderators or the funder of this bounty may do this.'))


def helper_handle_approvals(request, bounty):
    mutate_worker_action = request.GET.get('mutate_worker_action', None)
    mutate_worker_action_past_tense = 'approved' if mutate_worker_action == 'approve' else 'rejected'
    worker = request.GET.get('worker', None)

    if mutate_worker_action:
        if not request.user.is_authenticated:
            messages.warning(request, _('You must be logged in to approve or reject worker submissions. Please login and try again.'))
            return

        if not worker:
            messages.warning(request, _('You must provide the worker\'s username in order to approve or reject them.'))
            return

        is_funder = bounty.is_funder(request.user.username.lower())
        is_staff = request.user.is_staff
        if is_funder or is_staff:
            pending_interests = bounty.interested.select_related('profile').filter(profile__handle=worker.lower(), pending=True)
            # Check whether or not there are pending interests.
            if not pending_interests.exists():
                messages.warning(
                    request,
                    _('This worker does not exist or is not in a pending state. Perhaps they were already approved or rejected? Please check your link and try again.'))
                return
            interest = pending_interests.first()

            if mutate_worker_action == 'approve':
                interest.pending = False
                interest.acceptance_date = timezone.now()
                interest.save()

                start_work_approved(interest, bounty)

                maybe_market_to_github(bounty, 'work_started', profile_pairs=bounty.profile_pairs)
                maybe_market_to_slack(bounty, 'worker_approved')
                record_bounty_activity(bounty, request.user, 'worker_approved', interest)
                maybe_market_to_user_slack(bounty, 'worker_approved')
            else:
                start_work_rejected(interest, bounty)

                record_bounty_activity(bounty, request.user, 'worker_rejected', interest)
                bounty.interested.remove(interest)
                interest.delete()

                maybe_market_to_slack(bounty, 'worker_rejected')
                maybe_market_to_user_slack(bounty, 'worker_rejected')

            messages.success(request, _(f'{worker} has been {mutate_worker_action_past_tense}'))
        else:
            messages.warning(request, _('Only the funder of this bounty may perform this action.'))


def helper_handle_remarket_trigger(request, bounty):
    trigger_remarket = request.GET.get('trigger_remarket', False)
    if trigger_remarket:
        is_staff = request.user.is_staff
        is_funder = bounty.is_funder(request.user.username.lower())
        if is_staff or is_funder:
            result = re_market_bounty(bounty)
            if result['success']:
                base_result_msg = "This issue has been remarketed."
                messages.success(request, _(base_result_msg + " " + result['msg']))
            else:
                messages.warning(request, _(result['msg']))
        else:
            messages.warning(request, _('Only staff or the funder of this bounty may do this.'))


def helper_handle_release_bounty_to_public(request, bounty):
    release_to_public = request.GET.get('release_to_public', False)
    if release_to_public:
        is_bounty_status_reserved = bounty.status == 'reserved'
        if is_bounty_status_reserved:
            is_staff = request.user.is_staff
            is_bounty_reserved_for_user = bounty.reserved_for_user_handle == request.user.username.lower()
            if is_staff or is_bounty_reserved_for_user:
                success = release_bounty_to_the_public(bounty)
                if success:
                    messages.success(request, _('You have successfully released this bounty to the public'))
                else:
                    messages.warning(request, _('An error has occurred whilst trying to release. Please try again later'))
            else:
                messages.warning(request, _('Only staff or the user that has been reserved can release this bounty'))
        else:
            messages.warning(request, _('This functionality is only for reserved bounties'))



@login_required
def bounty_invite_url(request, invitecode):
    """Decode the bounty details and redirect to correct bounty

    Args:
        invitecode (str): Unique invite code with bounty details and handle

    Returns:
        django.template.response.TemplateResponse: The Bounty details template response.
    """
    try:
        decoded_data = get_bounty_from_invite_url(invitecode)
        bounty = Bounty.objects.current().filter(pk=decoded_data['bounty']).first()
        inviter = User.objects.filter(username=decoded_data['inviter']).first()
        bounty_invite = BountyInvites.objects.filter(
            bounty=bounty,
            inviter=inviter,
            invitee=request.user
        ).first()
        if bounty_invite:
            bounty_invite.status = 'accepted'
            bounty_invite.save()
        else:
            bounty_invite = BountyInvites.objects.create(
                status='accepted'
            )
            bounty_invite.bounty.add(bounty)
            bounty_invite.inviter.add(inviter)
            bounty_invite.invitee.add(request.user)
        return redirect('/funding/details/?url=' + bounty.github_url)
    except Exception as e:
        logger.debug(e)
        raise Http404


def bounty_details(request, ghuser='', ghrepo='', ghissue=0, stdbounties_id=None):
    """Display the bounty details.

    Args:
        ghuser (str): The Github user. Defaults to an empty string.
        ghrepo (str): The Github repository. Defaults to an empty string.
        ghissue (int): The Github issue number. Defaults to: 0.

    Raises:
        Exception: The exception is raised for any exceptions in the main query block.

    Returns:
        django.template.response.TemplateResponse: The Bounty details template response.

    """
    from .utils import clean_bounty_url
    is_user_authenticated = request.user.is_authenticated
    request_url = clean_bounty_url(request.GET.get('url', ''))
    if is_user_authenticated and hasattr(request.user, 'profile'):
        _access_token = request.user.profile.get_access_token()
    else:
        _access_token = request.session.get('access_token')

    try:
        if ghissue:
            issue_url = 'https://github.com/' + ghuser + '/' + ghrepo + '/issues/' + ghissue
            bounties = Bounty.objects.current().filter(github_url=issue_url)
            if not bounties.exists():
                issue_url = 'https://github.com/' + ghuser + '/' + ghrepo + '/pull/' + ghissue
                bounties = Bounty.objects.current().filter(github_url=issue_url)
        else:
            issue_url = request_url
            bounties = Bounty.objects.current().filter(github_url=issue_url)

    except Exception:
        pass

    params = {
        'issueURL': issue_url,
        'title': _('Issue Details'),
        'card_title': _('Funded Issue Details | Gitcoin'),
        'avatar_url': static('v2/images/helmet.png'),
        'active': 'bounty_details',
        'is_github_token_valid': is_github_token_valid(_access_token),
        'github_auth_url': get_auth_url(request.path),
        "newsletter_headline": _("Be the first to know about new funded issues."),
        'is_staff': request.user.is_staff,
        'is_moderator': request.user.profile.is_moderator if hasattr(request.user, 'profile') else False,
    }

    bounty = None

    if issue_url:
        try:
            if stdbounties_id is not None and stdbounties_id == "0":
                new_id = Bounty.objects.current().filter(github_url=issue_url).first().standard_bounties_id
                return redirect('issue_details_new3', ghuser=ghuser, ghrepo=ghrepo, ghissue=ghissue, stdbounties_id=new_id)
            if stdbounties_id and stdbounties_id.isdigit():
                stdbounties_id = clean_str(stdbounties_id)
                bounties = bounties.filter(standard_bounties_id=stdbounties_id)
            if bounties:
                bounty = bounties.order_by('-pk').first()
                if bounties.count() > 1 and bounties.filter(network='mainnet').count() > 1:
                    bounty = bounties.filter(network='mainnet').order_by('-pk').first()
                # Currently its not finding anyting in the database
                if bounty.title and bounty.org_name:
                    params['card_title'] = f'{bounty.title} | {bounty.org_name} Funded Issue Detail | Gitcoin'
                    params['title'] = clean(params['card_title'], strip=True)
                    params['card_desc'] = ellipses(clean(bounty.issue_description_text, strip=True), 255)
                    params['noscript'] = {
                        'title': clean(bounty.title, strip=True),
                        'org_name': bounty.org_name,
                        'issue_description_text': clean(bounty.issue_description_text, strip=True),
                        'keywords': ', '.join(bounty.keywords.split(','))}

                if bounty.event and bounty.event.slug:
                    params['event'] = bounty.event.slug

                params['bounty_pk'] = bounty.pk
                params['network'] = bounty.network
                params['stdbounties_id'] = bounty.standard_bounties_id if not stdbounties_id else stdbounties_id
                params['interested_profiles'] = bounty.interested.select_related('profile').all()
                params['avatar_url'] = bounty.get_avatar_url(True)
                params['canonical_url'] = bounty.canonical_url
                params['is_bounties_network'] = bounty.is_bounties_network

                if bounty.event:
                    params['event_tag'] = bounty.event.slug
                    params['prize_projects'] = HackathonProject.objects.filter(hackathon=bounty.event, bounty__standard_bounties_id=bounty.standard_bounties_id).exclude(status='invalid').prefetch_related('profiles')

                helper_handle_snooze(request, bounty)
                helper_handle_approvals(request, bounty)
                helper_handle_admin_override_and_hide(request, bounty)
                helper_handle_suspend_auto_approval(request, bounty)
                helper_handle_mark_as_remarket_ready(request, bounty)
                helper_handle_remarket_trigger(request, bounty)
                helper_handle_release_bounty_to_public(request, bounty)
                helper_handle_admin_contact_funder(request, bounty)
                helper_handle_override_status(request, bounty)
        except Bounty.DoesNotExist:
            pass
        except Exception as e:
            logger.error(e)

    if request.GET.get('sb') == '1' or (bounty and bounty.is_bounties_network):
        return TemplateResponse(request, 'bounty/details.html', params)

    return TemplateResponse(request, 'bounty/details2.html', params)


def funder_payout_reminder_modal(request, bounty_network, stdbounties_id):
    bounty = Bounty.objects.current().filter(network=bounty_network, standard_bounties_id=stdbounties_id).first()

    context = {
        'bounty': bounty,
        'active': 'funder_payout_reminder_modal',
        'title': _('Send Payout Reminder')
    }
    return TemplateResponse(request, 'funder_payout_reminder_modal.html', context)


@csrf_exempt
def funder_payout_reminder(request, bounty_network, stdbounties_id):
    if not request.user.is_authenticated:
        return JsonResponse(
            {'error': 'You must be authenticated via github to use this feature!'},
            status=401)

    if hasattr(request.user, 'profile'):
        access_token = request.user.profile.get_access_token()
    else:
        access_token = request.session.get('access_token')
    github_user_data = get_github_user_data(access_token)

    try:
        bounty = Bounty.objects.current().filter(network=bounty_network, standard_bounties_id=stdbounties_id).first()
    except Bounty.DoesNotExist:
        raise Http404

    has_fulfilled = bounty.fulfillments.filter(fulfiller_github_username=github_user_data['login']).count()
    if has_fulfilled == 0:
        return JsonResponse({
            'success': False,
          },
          status=403)

    #  410 Gone Indicates that the resource requested is no longer available and will not be available again.
    if bounty.funder_last_messaged_on:
        return JsonResponse({
            'success': False,
          },
          status=410)

    user = request.user
    funder_payout_reminder_mail(to_email=bounty.bounty_owner_email, bounty=bounty, github_username=user, live=True)
    bounty.funder_last_messaged_on = timezone.now()
    bounty.save()
    return JsonResponse({
          'success': True
        },
        status=200)


def quickstart(request):
    """Display Quickstart Guide."""

    activities = Activity.objects.filter(activity_type='new_bounty').order_by('-created')[:5]
    context = deepcopy(qs.quickstart)
    context["activities"] = activities
    context['avatar_url'] = request.build_absolute_uri(static('v2/images/twitter_cards/tw_cards-01.png'))
    return TemplateResponse(request, 'quickstart.html', context)


def load_banners(request):
    """Load profile banners"""
    images = load_files_in_directory('wallpapers')
    response = {
        'status': 200,
        'banners': images
    }
    return JsonResponse(response, safe=False)


def profile_details(request, handle):
    """Display profile keywords.

    Args:
        handle (str): The profile handle.

    """
    try:
        profile = profile_helper(handle, True)
    except (ProfileNotFoundException, ProfileHiddenException):
        raise Http404

    if not settings.DEBUG:
        network = 'mainnet'
    else:
        network = 'rinkeby'

    keywords = request.GET.get('keywords', '')

    bounties = Bounty.objects.current().prefetch_related(
        'fulfillments',
        'interested',
        'interested__profile',
        'feedbacks'
        ).filter(
            interested__profile=profile,
            network=network,
        ).filter(
            interested__status='okay'
        ).filter(
            interested__pending=False
        ).filter(
            idx_status='done'
        ).filter(
            feedbacks__receiver_profile=profile
        ).filter(
            Q(metadata__issueKeywords__icontains=keywords) |
            Q(title__icontains=keywords) |
            Q(issue_description__icontains=keywords)
        ).distinct('pk')[:3]

    _bounties = []
    _orgs = []
    if bounties :
        for bounty in bounties:

            _bounty = {
                'title': bounty.title,
                'id': bounty.id,
                'org': bounty.org_name,
                'rating': [feedback.rating for feedback in bounty.feedbacks.all().distinct('bounty_id')],
            }
            _org = bounty.org_name
            _orgs.append(_org)
            _bounties.append(_bounty)

    response = {
        'avatar': profile.avatar_url,
        'handle': profile.handle,
        'contributed_to': _orgs,
        'keywords': keywords,
        'related_bounties' : _bounties,
        'stats': {
            'position': profile.get_contributor_leaderboard_index(),
            'completed_bounties': profile.completed_bounties,
            'success_rate': profile.success_rate,
            'earnings': profile.get_eth_sum()
        }
    }

    return JsonResponse(response, safe=False)


def user_card(request, handle):
    """Display profile keywords.

    Args:
        handle (str): The profile handle.

    """
    try:
        profile = profile_helper(handle, True)
    except (ProfileNotFoundException, ProfileHiddenException):
        raise Http404

    if not settings.DEBUG:
        network = 'mainnet'
    else:
        network = 'rinkeby'

    if request.user.is_authenticated:
        is_following = True if TribeMember.objects.filter(profile=request.user.profile, org=profile).count() else False
    else:
        is_following = False

    profile_dict = profile.as_dict
    followers = TribeMember.objects.filter(org=profile).count()
    following = TribeMember.objects.filter(profile=profile).count()
    response = {
        'is_authenticated': request.user.is_authenticated,
        'is_following': is_following,
        'profile' : {
            'avatar_url': profile.avatar_url,
            'handle': profile.handle,
            'orgs' : profile.organizations,
            'created_on' : profile.created_on,
            'keywords' : profile.keywords,
            'data': profile.data,
            'followers':followers,
            'following':following,
        },
        'profile_dict':profile_dict
    }

    return JsonResponse(response, safe=False)


def profile_keywords(request, handle):
    """Display profile details.

    Args:
        handle (str): The profile handle.

    """
    try:
        profile = profile_helper(handle, True)
    except (ProfileNotFoundException, ProfileHiddenException):
        raise Http404

    response = {
        'status': 200,
        'keywords': profile.keywords,
    }
    return JsonResponse(response)


def profile_quests(request, handle):
    """Display profile quest points details.

    Args:
        handle (str): The profile handle.

    """
    try:
        profile = profile_helper(handle, True)
    except (ProfileNotFoundException, ProfileHiddenException):
        raise Http404

    from quests.models import QuestPointAward
    qpas = QuestPointAward.objects.filter(profile=profile).order_by('created_on')
    history = []

    response = """date,close"""
    balances = {}
    running_balance = 0
    for ele in qpas:
        val = ele.value
        if val:
            running_balance += val
            datestr = ele.created_on.strftime('%d-%b-%y')
            if datestr not in balances.keys():
                balances[datestr] = 0
            balances[datestr] = running_balance

    for datestr, balance in balances.items():
        response += f"\n{datestr},{balance}"

    mimetype = 'text/x-csv'
    return HttpResponse(response)



def profile_grants(request, handle):
    """Display profile grant contribution details.

    Args:
        handle (str): The profile handle.

    """
    try:
        profile = profile_helper(handle, True)
    except (ProfileNotFoundException, ProfileHiddenException):
        raise Http404

    from grants.models import Contribution
    contributions = Contribution.objects.filter(subscription__contributor_profile=profile).order_by('-pk')
    history = []

    response = """date,close"""
    balances = {}
    for ele in contributions:
        val = ele.normalized_data.get('amount_per_period_usdt')
        if val:
            datestr = ele.created_on.strftime('1-%b-%y')
            if datestr not in balances.keys():
                balances[datestr] = 0
            balances[datestr] += val

    for datestr, balance in balances.items():
        response += f"\n{datestr},{balance}"

    mimetype = 'text/x-csv'
    return HttpResponse(response)


def profile_activity(request, handle):
    """Display profile activity details.

    Args:
        handle (str): The profile handle.

    """
    try:
        profile = profile_helper(handle, True)
    except (ProfileNotFoundException, ProfileHiddenException):
        raise Http404

    activities = list(profile.get_various_activities().values_list('created_on', flat=True))
    activities += list(profile.actions.values_list('created_on', flat=True))
    response = {}
    prev_date = timezone.now()
    for i in range(1, 12*30):
        date = timezone.now() - timezone.timedelta(days=i)
        count = len([activity_date for activity_date in activities if (activity_date < prev_date and activity_date > date)])
        if count:
            response[int(date.timestamp())] = count
        prev_date = date
    return JsonResponse(response)


def profile_spent(request, handle):
    """Display profile spent details.

    Args:
        handle (str): The profile handle.

    """
    return profile_earnings(request, handle, 'from')


def profile_ratings(request, handle, attr):
    """Display profile ratings details.

    Args:
        handle (str): The profile handle.

    """
    try:
        profile = profile_helper(handle, True)
    except (ProfileNotFoundException, ProfileHiddenException):
        raise Http404

    response = """date,close"""
    items = list(profile.feedbacks_got.values_list('created_on', attr))
    balances = {}
    for ele in items:
        val = ele[1]
        if val and val > 0:
            datestr = ele[0].strftime('1-%b-%y')
            if datestr not in balances.keys():
                balances[datestr] = {'sum': 0, 'count':0}
            balances[datestr]['sum'] += val
            balances[datestr]['count'] += 1

    for datestr, balance in balances.items():
        balance = balance['sum'] / balance['count']
        response += f"\n{datestr},{balance}"

    mimetype = 'text/x-csv'
    return HttpResponse(response)


def profile_earnings(request, handle, direction='to'):
    """Display profile earnings details.

    Args:
        handle (str): The profile handle.

    """
    try:
        profile = profile_helper(handle, True)
    except (ProfileNotFoundException, ProfileHiddenException):
        raise Http404

    if not request.user.is_authenticated or profile.pk != request.user.profile.pk:
        raise Http404

    earnings = profile.earnings
    if direction == "from":
        earnings = profile.sent_earnings

    response = """date,close"""
    earnings = list(earnings.order_by('created_on').values_list('created_on', 'value_usd'))
    balances = {}
    for earning in earnings:
        val = earning[1]
        if val:
            datestr = earning[0].strftime('1-%b-%y')
            if datestr not in balances.keys():
                balances[datestr] = 0
            balances[datestr] += val

    for datestr, balance in balances.items():
        response += f"\n{datestr},{balance}"

    mimetype = 'text/x-csv'
    return HttpResponse(response)


def profile_viewers(request, handle):
    """Display profile viewers details.

    Args:
        handle (str): The profile handle.

    """
    try:
        profile = profile_helper(handle, True)
    except (ProfileNotFoundException, ProfileHiddenException):
        raise Http404

    if not request.user.is_authenticated or profile.pk != request.user.profile.pk:
        raise Http404

    response = """date,close"""
    items = list(profile.viewed_by.order_by('created_on').values_list('created_on', flat=True))
    balances = {}
    for item in items:
        datestr = item.strftime('%d-%b-%y')
        if datestr not in balances.keys():
            balances[datestr] = 0
        balances[datestr] += 1

    for datestr, balance in balances.items():
        response += f"\n{datestr},{balance}"

    mimetype = 'text/x-csv'
    return HttpResponse(response)


@require_POST
@login_required
def profile_job_opportunity(request, handle):
    """ Save profile job opportunity.

    Args:
        handle (str): The profile handle.
    """
    uploaded_file = request.FILES.get('job_cv')
    error_response = invalid_file_response(uploaded_file, supported=['application/pdf'])
    # 400 is ok because file upload is optional here
    if error_response and error_response['status'] != 400:
        return JsonResponse(error_response)
    try:
        profile = profile_helper(handle, True)
        if request.user.profile.id != profile.id:
            return JsonResponse(
                {'error': 'Bad request'},
                status=401)
        profile.job_search_status = request.POST.get('job_search_status', None)
        profile.show_job_status = request.POST.get('show_job_status', None) == 'true'
        profile.job_type = request.POST.get('job_type', None)
        profile.remote = request.POST.get('remote', None) == 'on'
        profile.job_salary = float(request.POST.get('job_salary', '0').replace(',', ''))
        profile.job_location = json.loads(request.POST.get('locations'))
        profile.linkedin_url = request.POST.get('linkedin_url', None)
        profile.resume = request.FILES.get('job_cv', profile.resume) if not error_response else None
        profile.save()
    except (ProfileNotFoundException, ProfileHiddenException):
        raise Http404

    response = {
        'status': 200,
        'message': 'Job search status saved'
    }
    return JsonResponse(response)

@require_POST
@login_required
def profile_settings(request):
    """ Toggle profile automatic backup flag.

    Args:
        handle (str): The profile handle.
    """

    handle = request.user.profile.handle

    try:
        profile = profile_helper(handle, True)
    except (ProfileNotFoundException, ProfileHiddenException):
        raise Http404

    if not request.user.is_authenticated or profile.pk != request.user.profile.pk:
        raise Http404

    profile.automatic_backup = not profile.automatic_backup
    profile.save()

    response = {
        'status': 200,
        'message': 'Profile automatic backup flag toggled',
        'automatic_backup': profile.automatic_backup
    }

    return JsonResponse(response, status=response.get('status', 200))

@require_POST
@login_required
def profile_backup(request):
    """ Read the profile backup data.

    Args:
        handle (str): The profile handle.
    """

    handle = request.user.profile.handle

    try:
        profile = profile_helper(handle, True)
    except (ProfileNotFoundException, ProfileHiddenException):
        raise Http404

    if not request.user.is_authenticated or profile.pk != request.user.profile.pk:
        raise Http404

    model = request.POST.get('model', None)
    # prepare the exported data for backup
    profile_data = ProfileExportSerializer(profile).data
    data = {}
    keys = ['grants', 'portfolio', 'active_work', 'bounties', 'activities',
        'tips', '_tips.private_fields', 'feedbacks', '_feedbacks.private_items',
        'custom_avatars'] + list(profile_data.keys())

    if model == 'custom avatar': # custom avatar
        custom_avatars = get_custom_avatars(profile)
        data['custom_avatars'] = CustomAvatarExportSerializer(custom_avatars, many=True).data
    elif model == 'grants': # grants
        data["grants"] = GrantExportSerializer(profile.get_my_grants, many=True).data
    elif model == 'portfolio': # portfolio, active work
        portfolio_bounties = profile.get_fulfilled_bounties()
        active_work = Bounty.objects.none()
        interests = profile.active_bounties
        for interest in interests:
            active_work = active_work | Bounty.objects.filter(interested=interest, current_bounty=True)
        data["portfolio"] = BountyExportSerializer(portfolio_bounties, many=True).data
        data["active_work"] = BountyExportSerializer(active_work, many=True).data
    elif model == 'bounties': # bounties
        data["bounties"] = BountyExportSerializer(profile.bounties, many=True).data
    elif model == 'activities': # activities
        data["activities"] = ActivityExportSerializer(profile.activities, many=True).data
    elif model == 'tips': # tips
        data['tips'] = filtered_list_data('tip', profile.tips, private_items=None, private_fields=False)
        data['_tips.private_fields'] = filtered_list_data('tip', profile.tips, private_items=None, private_fields=True)
    elif model == 'feedback': # feedback
        feedbacks = FeedbackEntry.objects.filter(receiver_profile=profile).all()
        data['feedbacks'] = filtered_list_data('feedback', feedbacks, private_items=False, private_fields=None)
        data['_feedbacks.private_items'] = filtered_list_data('feedback', feedbacks, private_items=True, private_fields=None)
    else:
        data = profile_data

    response = {
        'status': 200,
        'data': data,
        'keys': keys
    }

    return JsonResponse(response, status=response.get('status', 200))

def invalid_file_response(uploaded_file, supported):
    response = None
    forbidden_content = ['<script>']
    if not uploaded_file:
        response = {
            'status': 400,
            'message': 'No File Found'
        }
    elif uploaded_file.size > 31457280:
        # 30MB max file size
        response = {
            'status': 413,
            'message': 'File Too Large'
        }
    else:
        file_mime = magic.from_buffer(next(uploaded_file.chunks()), mime=True)
        logger.info('uploaded file: %s' % file_mime)
        if file_mime not in supported:
            response = {
                'status': 415,
                'message': 'Invalid File Type'
            }
        '''
        try:
            forbidden = False
            while forbidden is False:
                chunk = next(uploaded_file.chunks())
                if not chunk:
                    break
                for ele in forbidden_content:
                    # could add in other ways to determine forbidden content
                    q = ele.encode('ascii')

                    if chunk.find(q) != -1:
                        forbidden = True
                        response = {
                            'status': 422,
                            'message': 'Invalid File contents'
                        }
                        break

        except Exception as e:
            print(e)
        '''

    return response


def get_profile_tab(request, profile, tab, prev_context):

    #config
    if not settings.DEBUG:
        network = 'mainnet'
    else:
        network = 'rinkeby'
    status = 200
    order_by = request.GET.get('order_by', '-modified_on')
    context = profile.reassemble_profile_dict

    # all tabs
    active_bounties = context['active_bounties'].order_by('-web3_created')
    context['active_bounties_count'] = active_bounties.cache().count()
    context['portfolio_count'] = len(context['portfolio']) + profile.portfolio_items.cache().count()
    context['projects_count'] = HackathonProject.objects.filter( profiles__id=profile.id).cache().count()
    context['my_kudos'] = profile.get_my_kudos.cache().distinct('kudos_token_cloned_from__name')[0:7]
    # specific tabs
    if tab == 'activity':
        all_activities = ['all', 'new_bounty', 'start_work', 'work_submitted', 'work_done', 'new_tip', 'receive_tip', 'new_grant', 'update_grant', 'killed_grant', 'new_grant_contribution', 'new_grant_subscription', 'killed_grant_contribution', 'receive_kudos', 'new_kudos', 'joined', 'updated_avatar']
        activity_tabs = [
            (_('All Activity'), all_activities),
            (_('Bounties'), ['new_bounty', 'start_work', 'work_submitted', 'work_done']),
            (_('Tips'), ['new_tip', 'receive_tip']),
            (_('Kudos'), ['receive_kudos', 'new_kudos']),
            (_('Grants'), ['new_grant', 'update_grant', 'killed_grant', 'new_grant_contribution', 'new_grant_subscription', 'killed_grant_contribution']),
        ]
        if profile.is_org:
            activity_tabs = [
                (_('All Activity'), all_activities),
                ]

        page = request.GET.get('p', None)

        if page:
            page = int(page)
            activity_type = request.GET.get('a', '')
            if activity_type == 'currently_working':
                currently_working_bounties = Bounty.objects.current().filter(interested__profile=profile).filter(interested__status='okay') \
                    .filter(interested__pending=False).filter(idx_status__in=Bounty.WORK_IN_PROGRESS_STATUSES)
                currently_working_bounties_count = currently_working_bounties.count()
                if currently_working_bounties_count > 0:
                    paginator = Paginator(currently_working_bounties, 10)

                if page > paginator.num_pages:
                    return HttpResponse(status=204)

                context = {}
                context['bounties'] = [bounty for bounty in paginator.get_page(page)]

                return TemplateResponse(request, 'profiles/profile_bounties.html', context, status=status)

            else:

                all_activities = profile.get_various_activities()
                paginator = Paginator(profile_filter_activities(all_activities, activity_type, activity_tabs), 10)

                if page > paginator.num_pages:
                    return HttpResponse(status=204)

                context = {}
                context['activities'] = [ele.view_props_for(request.user) for ele in paginator.get_page(page)]

                return TemplateResponse(request, 'profiles/profile_activities.html', context, status=status)


        all_activities = context.get('activities')
        tabs = []
        counts = context.get('activities_counts', {'joined': 1})
        for name, actions in activity_tabs:

            # this functions as profile_filter_activities does
            # except w. aggregate counts
            activities_count = 0
            for action in actions:
                activities_count += counts.get(action, 0)


            # dont draw a tab where the activities count is 0
            if activities_count == 0:
                continue

            # buidl dict
            obj = {'id': slugify(name),
                   'name': name,
                   'objects': [],
                   'count': activities_count,
                   'type': 'activity'
                   }
            tabs.append(obj)

            context['tabs'] = tabs

        if request.method == 'POST' and request.is_ajax():
            # Update profile address data when new preferred address is sent
            validated = request.user.is_authenticated and request.user.username.lower() == profile.handle.lower()
            if validated and request.POST.get('address'):
                address = request.POST.get('address')
                profile.preferred_payout_address = address
                profile.save()
                msg = {
                    'status': 200,
                    'msg': _('Success!'),
                    'wallets': [profile.preferred_payout_address, ],
                }

                return JsonResponse(msg, status=msg.get('status', 200))
    elif tab == 'orgs':
        pass
    elif tab == 'tribe':
        context['team'] = profile.team_or_none_if_timeout
    elif tab == 'follow':
        pass
    elif tab == 'people':
        if profile.is_org:
            context['team'] = profile.team_or_none_if_timeout
    elif tab == 'hackathons':
        context['projects'] = HackathonProject.objects.filter( profiles__id=profile.id)
    elif tab == 'quests':
        context['quest_wins'] = profile.quest_attempts.filter(success=True)
    elif tab == 'grants':
        from grants.models import Contribution
        contributions = Contribution.objects.filter(subscription__contributor_profile=profile).order_by('-pk')
        history = []
        for ele in contributions:
            history.append(ele.normalized_data)
        context['history'] = history
    elif tab == 'active':
        context['active_bounties'] = active_bounties
    elif tab == 'resume':
        if not prev_context['is_editable'] and not profile.show_job_status:
            raise Http404
    elif tab == 'viewers':
        if not prev_context['is_editable']:
            raise Http404
        pass
    elif tab == 'portfolio':
        title = request.POST.get('project_title')
        if title:
            if request.POST.get('URL')[0:4] != "http":
                messages.error(request, 'Invalid link.')
            elif not request.POST.get('URL')[0:4]:
                messages.error(request, 'Please enter some tags.')
            elif not request.user.is_authenticated or request.user.profile.pk != profile.pk:
                messages.error(request, 'Not Authorized')
            else:
                PortfolioItem.objects.create(
                    profile=request.user.profile,
                    title=title,
                    link=request.POST.get('URL'),
                    tags=request.POST.get('tags').split(','),
                    )
                messages.info(request, 'Portfolio Item added.')
    elif tab == 'earnings':
        context['earnings'] = Earning.objects.filter(to_profile=profile, network='mainnet', value_usd__isnull=False).order_by('-created_on')
    elif tab == 'spent':
        context['spent'] = Earning.objects.filter(from_profile=profile, network='mainnet', value_usd__isnull=False).order_by('-created_on')
    elif tab == 'kudos':
        context['org_kudos'] = profile.get_org_kudos
        owned_kudos = profile.get_my_kudos.order_by('id', order_by)
        sent_kudos = profile.get_sent_kudos.order_by('id', order_by)
        kudos_limit = 8
        context['kudos'] = owned_kudos[0:kudos_limit]
        context['sent_kudos'] = sent_kudos[0:kudos_limit]
        context['kudos_count'] = owned_kudos.count()
        context['sent_kudos_count'] = sent_kudos.count()

    elif tab == 'ratings':
        context['feedbacks_sent'] = [fb for fb in profile.feedbacks_sent.all() if fb.visible_to(request.user)]
        context['feedbacks_got'] = [fb for fb in profile.feedbacks_got.all() if fb.visible_to(request.user)]
        context['unrated_funded_bounties'] = Bounty.objects.current().prefetch_related('fulfillments', 'interested', 'interested__profile', 'feedbacks') \
            .filter(
                bounty_owner_github_username__iexact=profile.handle,
                network=network,
            ).exclude(
                feedbacks__feedbackType='approver',
                feedbacks__sender_profile=profile,
            ).distinct('pk').nocache()
        context['unrated_contributed_bounties'] = Bounty.objects.current().prefetch_related('feedbacks').filter(interested__profile=profile, network=network,) \
                .filter(interested__status='okay') \
                .filter(interested__pending=False).filter(idx_status='submitted') \
                .exclude(
                    feedbacks__feedbackType='worker',
                    feedbacks__sender_profile=profile
                ).distinct('pk').nocache()
    else:
        raise Http404
    return context

def profile_filter_activities(activities, activity_name, activity_tabs):
    """A helper function to filter a ActivityQuerySet.

    Args:
        activities (ActivityQuerySet): The ActivityQuerySet.
        activity_name (str): The activity_type to filter.

    Returns:
        ActivityQuerySet: The filtered results.

    """
    if not activity_name or activity_name == 'all-activity':
        return activities
    for name, actions in activity_tabs:
        if slugify(name) == activity_name:
            return activities.filter(activity_type__in=actions)
    return activities.filter(activity_type=activity_name)


def profile(request, handle, tab=None):
    """Display profile details.

    Args:
        handle (str): The profile handle.

    Variables:
        context (dict): The template context to be used for template rendering.
        profile (dashboard.models.Profile): The Profile object to be used.
        status (int): The status code of the response.

    Returns:
        TemplateResponse: The profile templated view.

    """

    # setup
    status = 200
    default_tab = 'activity'
    tab = tab if tab else default_tab
    handle = handle.replace("@", "")
    # perf
    disable_cache = False

    # make sure tab param is correct
    all_tabs = ['bounties', 'projects', 'manage', 'active', 'ratings', 'follow', 'portfolio', 'viewers', 'activity', 'resume', 'kudos', 'earnings', 'spent', 'orgs', 'people', 'grants', 'quests', 'tribe', 'hackathons']
    tab = default_tab if tab not in all_tabs else tab
    if handle in all_tabs and request.user.is_authenticated:
        # someone trying to go to their own profile?
        tab = handle
        handle = request.user.profile.handle

    # user only tabs
    if not handle and request.user.is_authenticated:
        handle = request.user.username
    is_my_profile = request.user.is_authenticated and request.user.username.lower() == handle.lower()
    user_only_tabs = ['viewers', 'earnings', 'spent']
    tab = default_tab if tab in user_only_tabs and not is_my_profile else tab

    context = {}
    # get this user
    try:
        if not handle and not request.user.is_authenticated:
            return redirect('funder_bounties')
        if not handle:
            handle = request.user.username
            profile = None
            if not profile:
                profile = profile_helper(handle, disable_cache=disable_cache, full_profile=True)
        else:
            if handle.endswith('/'):
                handle = handle[:-1]
            profile = profile_helper(handle, current_user=request.user, disable_cache=disable_cache)

    except (Http404, ProfileHiddenException, ProfileNotFoundException):
        status = 404
        context = {
            'hidden': True,
            'ratings': range(0,5),
            'followers': TribeMember.objects.filter(org=request.user.profile) if request.user.is_authenticated and hasattr(request.user, 'profile') else [],
            'profile': {
                'handle': handle,
                'avatar_url': f"/dynamic/avatar/Self",
                'data': {
                    'name': f"@{handle}",
                },
            },
        }
        return TemplateResponse(request, 'profiles/profile.html', context, status=status)

    # make sure we're on the right profile route + redirect if we dont
    if request.path not in profile.url and tab == default_tab:
        return redirect(profile.url)

    # setup context for visit
    if not len(profile.tribe_members) and tab == 'tribe':
        tab = 'activity'

    # hack to make sure that if ur looking at ur own profile u see right online status
    # previously this was cached on the session object, and we've not yet found a way to buste that cache
    if request.user.is_authenticated:
        if request.user.username.lower() == profile.handle:
            base = Profile.objects_full
            if disable_cache:
                base = base.nocache()
            profile = base.get(pk=profile.pk)

    context['is_my_org'] = request.user.is_authenticated and any(
        [handle.lower() == org.lower() for org in request.user.profile.organizations])
    if request.user.is_authenticated and hasattr(request.user, 'profile'):
        context['is_on_tribe'] = request.user.profile.tribe_members.filter(org__handle=handle.lower()).exists()
    else:
        context['is_on_tribe'] = False

    if profile.is_org and profile.handle.lower() in ['gitcoinco']:

        active_tab = 0
        if tab == "townsquare":
            active_tab = 0
        elif tab == "projects":
            active_tab = 1
        elif tab == "people":
            active_tab = 2
        elif tab == "bounties":
            active_tab = 3
        context['active_panel'] = active_tab

        # record profile view
        if request.user.is_authenticated and not context['is_my_org']:
            ProfileView.objects.create(target=profile, viewer=request.user.profile)
        try:
            network = get_default_network()
            orgs_bounties = profile.get_orgs_bounties(network=network)
            context['count_bounties_on_repo'] = orgs_bounties.count()
            context['sum_eth_on_repos'] = profile.get_eth_sum(bounties=orgs_bounties)
            context['works_with_org'] = profile.get_who_works_with(work_type='org', bounties=orgs_bounties)
            context['currentProfile'] = TribesSerializer(profile, context={'request': request}).data
            context['target'] = f'/activity?what=tribe:{profile.handle}'
            context['is_on_tribe'] = json.dumps(context['is_on_tribe'])
            context['is_my_org'] = json.dumps(context['is_my_org'])
            context['profile_handle'] = profile.handle

            return TemplateResponse(request, 'profiles/tribes-vue.html', context, status=status)
        except Exception as e:
            logger.info(str(e))


    if tab == 'tribe':
        context['tribe_priority'] = profile.tribe_priority
        suggested_bounties = BountyRequest.objects.filter(tribe=profile, status='o').order_by('created_on')
        if suggested_bounties:
            context['suggested_bounties'] = suggested_bounties

    context['is_my_profile'] = is_my_profile
    context['show_resume_tab'] = profile.show_job_status or context['is_my_profile']
    context['show_follow_tab'] = True
    context['is_editable'] = context['is_my_profile'] # or context['is_my_org']
    context['tab'] = tab
    context['show_activity'] = request.GET.get('p', False) != False

    context['ratings'] = range(0,5)
    context['feedbacks_sent'] = [fb.pk for fb in profile.feedbacks_sent.all() if fb.visible_to(request.user)]
    context['feedbacks_got'] = [fb.pk for fb in profile.feedbacks_got.all() if fb.visible_to(request.user)]
    context['all_feedbacks'] = context['feedbacks_got'] + context['feedbacks_sent']
    context['tags'] = [('#announce','bullhorn'), ('#mentor','terminal'), ('#jobs','code'), ('#help','laptop-code'), ('#other','briefcase'), ]
    context['followers'] = TribeMember.objects.filter(org=request.user.profile) if request.user.is_authenticated else TribeMember.objects.none()
    context['following'] = TribeMember.objects.filter(profile=request.user.profile) if request.user.is_authenticated else TribeMember.objects.none()
    context['foltab'] = request.GET.get('sub', 'followers')

    tab = get_profile_tab(request, profile, tab, context)
    if type(tab) == dict:
        context.update(tab)
    else:
        return tab

    # record profile view
    if request.user.is_authenticated and not context['is_my_profile']:
        ProfileView.objects.create(target=profile, viewer=request.user.profile)


    return TemplateResponse(request, 'profiles/profile.html', context, status=status)


@staff_member_required
def funders_mailing_list(request):
    profile_list = list(Profile.objects.filter(
        persona_is_funder=True).exclude(email="").values_list('email',
                                                              flat=True))
    return JsonResponse({'funder_emails': profile_list})


@staff_member_required
def hunters_mailing_list(request):
    profile_list = list(Profile.objects.filter(
        persona_is_hunter=True).exclude(email="").values_list('email',
                                                              flat=True))
    return JsonResponse({'hunter_emails': profile_list})


@csrf_exempt
def lazy_load_kudos(request):
    page = request.POST.get('page', 1)
    context = {}
    datarequest = request.POST.get('request')
    order_by = request.GET.get('order_by', '-modified_on')
    limit = int(request.GET.get('limit', 8))
    handle = request.POST.get('handle')

    if handle:
        try:
            profile = Profile.objects.get(handle=handle.lower())
            if datarequest == 'mykudos':
                key = 'kudos'
                context[key] = profile.get_my_kudos.order_by('id', order_by)
            else:
                key = 'sent_kudos'
                context[key] = profile.get_sent_kudos.order_by('id', order_by)
        except Profile.DoesNotExist:
            pass

    paginator = Paginator(context[key], limit)
    kudos = paginator.get_page(page)
    html_context = {}
    html_context[key] = kudos
    html_context['kudos_data'] = key
    kudos_html = loader.render_to_string('shared/kudos_card_profile.html', html_context)
    return JsonResponse({'kudos_html': kudos_html, 'has_next': kudos.has_next()})


@csrf_exempt
@ratelimit(key='ip', rate='5/m', method=ratelimit.UNSAFE, block=True)
def get_quickstart_video(request):
    """Show quickstart video."""
    context = {
        'active': 'video',
        'title': _('Quickstart Video'),
    }
    return TemplateResponse(request, 'quickstart_video.html', context)


@csrf_exempt
@ratelimit(key='ip', rate='5/m', method=ratelimit.UNSAFE, block=True)
def extend_issue_deadline(request):
    """Show quickstart video."""
    bounty = Bounty.objects.get(pk=request.GET.get("pk"))
    context = {
        'active': 'extend_issue_deadline',
        'title': _('Extend Expiration'),
        'bounty': bounty,
        'user_logged_in': request.user.is_authenticated,
        'login_link': '/login/github?next=' + request.GET.get('redirect', '/')
    }
    return TemplateResponse(request, 'extend_issue_deadline.html', context)


@require_POST
@csrf_exempt
@ratelimit(key='ip', rate='5/s', method=ratelimit.UNSAFE, block=True)
def sync_web3(request):
    """Sync up web3 with the database.

    This function has a few different uses.  It is typically called from the
    front end using the javascript `sync_web3` function.  The `issueURL` is
    passed in first, followed optionally by a `bountydetails` argument.

    Returns:
        JsonResponse: The JSON response following the web3 sync.

    """
    # setup
    result = {
        'status': '400',
        'msg': "bad request"
    }

    issue_url = request.POST.get('url')
    txid = request.POST.get('txid')
    network = request.POST.get('network')

    if issue_url and txid and network:
        # confirm txid has mined
        print('* confirming tx has mined')
        if not has_tx_mined(txid, network):
            result = {
                'status': '400',
                'msg': 'tx has not mined yet'
            }
        else:

            # get bounty id
            print('* getting bounty id')
            bounty_id = get_bounty_id(issue_url, network)
            if not bounty_id:
                result = {
                    'status': '400',
                    'msg': 'could not find bounty id'
                }
            else:
                # get/process bounty
                print('* getting bounty')
                bounty = get_bounty(bounty_id, network)
                print('* processing bounty')
                did_change = False
                max_tries_attempted = False
                counter = 0
                url = None
                while not did_change and not max_tries_attempted:
                    did_change, _, new_bounty = web3_process_bounty(bounty)
                    if not did_change:
                        print("RETRYING")
                        time.sleep(3)
                        counter += 1
                        max_tries_attempted = counter > 3
                    if new_bounty:
                        url = new_bounty.url
                        try:
                            fund_ables = Activity.objects.filter(activity_type='status_update',
                                                                 bounty=None,
                                                                 metadata__fund_able=True,
                                                                 metadata__resource__contains={
                                                                     'provider': issue_url
                                                                 })
                            if fund_ables.exists():
                                comment = f'New Bounty created {new_bounty.get_absolute_url()}'
                                activity = fund_ables.first()
                                activity.bounty = new_bounty
                                activity.save()
                                Comment.objects.create(profile=new_bounty.bounty_owner_profile, activity=activity, comment=comment)
                        except Activity.DoesNotExist as e:
                            print(e)

                result = {
                    'status': '200',
                    'msg': "success",
                    'did_change': did_change,
                    'url': url,
                }

    return JsonResponse(result, status=result['status'])


# LEGAL
@xframe_options_exempt
def terms(request):
    context = {
        'title': _('Terms of Use'),
    }
    return TemplateResponse(request, 'legal/terms.html', context)

def privacy(request):
    return TemplateResponse(request, 'legal/privacy.html', {})


def cookie(request):
    return TemplateResponse(request, 'legal/privacy.html', {})


def prirp(request):
    return TemplateResponse(request, 'legal/privacy.html', {})


def apitos(request):
    return TemplateResponse(request, 'legal/privacy.html', {})


def labs(request):
    labs = LabsResearch.objects.all()
    tools = Tool.objects.prefetch_related('votes').filter(category=Tool.CAT_ALPHA)

    socials = [{
        "name": _("GitHub Repo"),
        "link": "https://github.com/gitcoinco/labs/",
        "class": "fab fa-github fa-2x"
    }, {
        "name": _("Slack"),
        "link": "https://gitcoin.co/slack",
        "class": "fab fa-slack fa-2x"
    }, {
        "name": _("Contact the Team"),
        "link": "mailto:founders@gitcoin.co",
        "class": "fa fa-envelope fa-2x"
    }]

    context = {
        'active': "labs",
        'title': _("Labs"),
        'card_desc': _("Gitcoin Labs provides advanced tools for busy developers"),
        'avatar_url': 'https://c.gitcoin.co/labs/Articles-Announcing_Gitcoin_Labs.png',
        'tools': tools,
        'labs': labs,
        'socials': socials
    }
    return TemplateResponse(request, 'labs.html', context)


@csrf_exempt
@ratelimit(key='ip', rate='5/m', method=ratelimit.UNSAFE, block=True)
def redeem_coin(request, shortcode):
    if request.body:
        status = 'OK'

        body_unicode = request.body.decode('utf-8')
        body = json.loads(body_unicode)
        address = body['address']

        try:
            coin = CoinRedemption.objects.get(shortcode=shortcode)
            address = Web3.toChecksumAddress(address)

            if hasattr(coin, 'coinredemptionrequest'):
                status = 'error'
                message = 'Bad request'
            else:
                abi = json.loads('[{"constant":true,"inputs":[],"name":"mintingFinished","outputs":[{"name":"","type":"bool"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":true,"inputs":[],"name":"name","outputs":[{"name":"","type":"string"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":false,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"payable":false,"stateMutability":"nonpayable","type":"function"},{"constant":true,"inputs":[],"name":"totalSupply","outputs":[{"name":"","type":"uint256"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":false,"inputs":[{"name":"_from","type":"address"},{"name":"_to","type":"address"},{"name":"_value","type":"uint256"}],"name":"transferFrom","outputs":[{"name":"","type":"bool"}],"payable":false,"stateMutability":"nonpayable","type":"function"},{"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":false,"inputs":[{"name":"_to","type":"address"},{"name":"_amount","type":"uint256"}],"name":"mint","outputs":[{"name":"","type":"bool"}],"payable":false,"stateMutability":"nonpayable","type":"function"},{"constant":true,"inputs":[],"name":"version","outputs":[{"name":"","type":"string"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":false,"inputs":[{"name":"_spender","type":"address"},{"name":"_subtractedValue","type":"uint256"}],"name":"decreaseApproval","outputs":[{"name":"","type":"bool"}],"payable":false,"stateMutability":"nonpayable","type":"function"},{"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":false,"inputs":[],"name":"finishMinting","outputs":[{"name":"","type":"bool"}],"payable":false,"stateMutability":"nonpayable","type":"function"},{"constant":true,"inputs":[],"name":"owner","outputs":[{"name":"","type":"address"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":true,"inputs":[],"name":"symbol","outputs":[{"name":"","type":"string"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":false,"inputs":[{"name":"_to","type":"address"},{"name":"_value","type":"uint256"}],"name":"transfer","outputs":[{"name":"","type":"bool"}],"payable":false,"stateMutability":"nonpayable","type":"function"},{"constant":false,"inputs":[{"name":"_spender","type":"address"},{"name":"_addedValue","type":"uint256"}],"name":"increaseApproval","outputs":[{"name":"","type":"bool"}],"payable":false,"stateMutability":"nonpayable","type":"function"},{"constant":true,"inputs":[{"name":"_owner","type":"address"},{"name":"_spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"payable":false,"stateMutability":"view","type":"function"},{"constant":false,"inputs":[{"name":"newOwner","type":"address"}],"name":"transferOwnership","outputs":[],"payable":false,"stateMutability":"nonpayable","type":"function"},{"payable":false,"stateMutability":"nonpayable","type":"fallback"},{"anonymous":false,"inputs":[{"indexed":true,"name":"to","type":"address"},{"indexed":false,"name":"amount","type":"uint256"}],"name":"Mint","type":"event"},{"anonymous":false,"inputs":[],"name":"MintFinished","type":"event"},{"anonymous":false,"inputs":[{"indexed":true,"name":"previousOwner","type":"address"},{"indexed":true,"name":"newOwner","type":"address"}],"name":"OwnershipTransferred","type":"event"},{"anonymous":false,"inputs":[{"indexed":true,"name":"owner","type":"address"},{"indexed":true,"name":"spender","type":"address"},{"indexed":false,"name":"value","type":"uint256"}],"name":"Approval","type":"event"},{"anonymous":false,"inputs":[{"indexed":true,"name":"from","type":"address"},{"indexed":true,"name":"to","type":"address"},{"indexed":false,"name":"value","type":"uint256"}],"name":"Transfer","type":"event"}]')

                # Instantiate Colorado Coin contract
                contract = w3.eth.contract(coin.contract_address, abi=abi)

                tx = contract.functions.transfer(address, coin.amount * 10**18).buildTransaction({
                    'nonce': w3.eth.getTransactionCount(settings.COLO_ACCOUNT_ADDRESS),
                    'gas': 100000,
                    'gasPrice': recommend_min_gas_price_to_confirm_in_time(5) * 10**9
                })

                signed = w3.eth.account.signTransaction(tx, settings.COLO_ACCOUNT_PRIVATE_KEY)
                transaction_id = w3.eth.sendRawTransaction(signed.rawTransaction).hex()

                CoinRedemptionRequest.objects.create(
                    coin_redemption=coin,
                    ip=get_ip(request),
                    sent_on=timezone.now(),
                    txid=transaction_id,
                    txaddress=address
                )

                message = transaction_id
        except CoinRedemption.DoesNotExist:
            status = 'error'
            message = _('Bad request')
        except Exception as e:
            status = 'error'
            message = str(e)

        # http response
        response = {
            'status': status,
            'message': message,
        }

        return JsonResponse(response)

    try:
        coin = CoinRedemption.objects.get(shortcode=shortcode)

        params = {
            'class': 'redeem',
            'title': _('Coin Redemption'),
            'coin_status': _('PENDING')
        }

        try:
            coin_redeem_request = CoinRedemptionRequest.objects.get(coin_redemption=coin)
            params['colo_txid'] = coin_redeem_request.txid
        except CoinRedemptionRequest.DoesNotExist:
            params['coin_status'] = _('INITIAL')

        return TemplateResponse(request, 'yge/redeem_coin.html', params)
    except CoinRedemption.DoesNotExist:
        raise Http404


@login_required
def new_bounty(request):
    """Create a new bounty."""
    from .utils import clean_bounty_url

    events = HackathonEvent.objects.filter(end_date__gt=datetime.today())
    suggested_developers = []
    if request.user.is_authenticated:
        suggested_developers = BountyFulfillment.objects.prefetch_related('bounty')\
            .filter(
                bounty__bounty_owner_github_username__iexact=request.user.profile.handle,
                bounty__idx_status='done'
            ).values('fulfiller_github_username', 'profile__id').annotate(fulfillment_count=Count('bounty')) \
            .order_by('-fulfillment_count')[:5]
    bounty_params = {
        'newsletter_headline': _('Be the first to know about new funded issues.'),
        'issueURL': clean_bounty_url(request.GET.get('source') or request.GET.get('url', '')),
        'amount': request.GET.get('amount'),
        'events': events,
        'suggested_developers': suggested_developers
    }

    params = get_context(
        user=request.user if request.user.is_authenticated else None,
        confirm_time_minutes_target=confirm_time_minutes_target,
        active='submit_bounty',
        title=_('Create Funded Issue'),
        update=bounty_params,
    )
    params['blocked_urls'] = json.dumps(list(BlockedURLFilter.objects.all().values_list('expression', flat=True)))
    params['FEE_PERCENTAGE'] = request.user.profile.fee_percentage if request.user.is_authenticated else 10

    coupon_code = request.GET.get('coupon', False)
    if coupon_code:
        coupon = Coupon.objects.get(code=coupon_code)
        if coupon.expiry_date > datetime.now().date():
            params['FEE_PERCENTAGE'] = coupon.fee_percentage
            params['coupon_code'] = coupon.code
        else:
            params['expired_coupon'] = True

    activity_ref = request.GET.get('activity', False)
    try:
        activity_id = int(activity_ref)
        params['activity'] = Activity.objects.get(id=activity_id)
    except (ValueError, Activity.DoesNotExist):
        pass

    params['avatar_url'] = request.build_absolute_uri(static('v2/images/twitter_cards/tw_cards-01.png'))
    return TemplateResponse(request, 'bounty/fund.html', params)


@csrf_exempt
def get_suggested_contributors(request):
    previously_worked_developers = []
    users_invite = []
    keywords = request.GET.get('keywords', '').split(',')
    invitees = [int(x) for x in request.GET.get('invite', '').split(',') if x]

    if request.user.is_authenticated:
        previously_worked_developers = BountyFulfillment.objects.prefetch_related('bounty', 'profile')\
            .filter(
                bounty__bounty_owner_github_username__iexact=request.user.profile.handle,
                bounty__idx_status='done'
            ).values('fulfiller_github_username', 'profile__id').annotate(fulfillment_count=Count('bounty')) \
            .order_by('-fulfillment_count')

    keywords_filter = Q()
    for keyword in keywords:
        keywords_filter = keywords_filter | Q(bounty__metadata__issueKeywords__icontains=keyword) | \
        Q(bounty__title__icontains=keyword) | \
        Q(bounty__issue_description__icontains=keyword)

    recommended_developers = BountyFulfillment.objects.prefetch_related('bounty', 'profile') \
        .filter(keywords_filter).values('fulfiller_github_username', 'profile__id') \
        .exclude(fulfiller_github_username__isnull=True) \
        .exclude(fulfiller_github_username__exact='').distinct()[:10]

    verified_developers = UserVerificationModel.objects.filter(verified=True).values('user__profile__handle', 'user__profile__id')

    if invitees:
        invitees_filter = Q()
        for invite in invitees:
            invitees_filter = invitees_filter | Q(pk=invite)

        users_invite = Profile.objects.filter(invitees_filter).values('id', 'handle', 'email').distinct()

    return JsonResponse(
                {
                    'contributors': list(previously_worked_developers),
                    'recommended_developers': list(recommended_developers),
                    'verified_developers': list(verified_developers),
                    'invites': list(users_invite)
                },
                status=200)


@csrf_exempt
@login_required
@ratelimit(key='ip', rate='5/m', method=ratelimit.UNSAFE, block=True)
def change_bounty(request, bounty_id):
    # ETC-TODO: ADD UPDATE AMOUNT for ETC
    user = request.user if request.user.is_authenticated else None

    if not user:
        if request.body:
            return JsonResponse(
                {'error': _('You must be authenticated via github to use this feature!')},
                status=401)
        else:
            return redirect('/login/github?next=' + request.get_full_path())

    try:
        bounty_id = int(bounty_id)
        bounty = Bounty.objects.get(pk=bounty_id)
    except:
        if request.body:
            return JsonResponse({'error': _('Bounty doesn\'t exist!')}, status=404)
        else:
            raise Http404

    keys = [
        'title',
        'experience_level',
        'project_length',
        'bounty_type',
        'featuring_date',
        'bounty_categories',
        'issue_description',
        'permission_type',
        'project_type',
        'reserved_for_user_handle',
        'is_featured',
        'admin_override_suspend_auto_approval',
        'keywords'
    ]

    if request.body:
        can_change = (bounty.status in Bounty.OPEN_STATUSES) or \
                (bounty.can_submit_after_expiration_date and bounty.status is 'expired')
        if not can_change:
            return JsonResponse({
                'error': _('The bounty can not be changed anymore.')
            }, status=405)

        is_funder = bounty.is_funder(user.username.lower()) if user else False
        is_staff = request.user.is_staff if user else False
        if not is_funder and not is_staff:
            return JsonResponse({
                'error': _('You are not authorized to change the bounty.')
            }, status=401)

        try:
            params = json.loads(request.body)
        except Exception:
            return JsonResponse({'error': 'Invalid JSON.'}, status=400)

        bounty_changed = False
        new_reservation = False
        for key in keys:
            value = params.get(key, 0)
            if value != 0:
                if key == 'featuring_date':
                    value = timezone.make_aware(
                        timezone.datetime.fromtimestamp(int(value)),
                        timezone=UTC)

                if key == 'bounty_categories':
                    value = value.split(',')
                old_value = getattr(bounty, key)

                if value != old_value:
                    if key == 'keywords':
                        bounty.metadata['issueKeywords'] = value
                    else:
                        setattr(bounty, key, value)
                    bounty_changed = True
                    if key == 'reserved_for_user_handle' and value:
                        new_reservation = True

        bounty_increased = False
        if not bounty.is_bounties_network:
            current_amount = float(bounty.value_true)
            new_amount = float(params.get('amount')) if params.get('amount') else None
            if new_amount and current_amount != new_amount:
                bounty.value_true = new_amount
                value_in_token = params.get('value_in_token')
                bounty.value_in_token = value_in_token
                bounty.balance = value_in_token
                try:
                    bounty.token_value_in_usdt = convert_token_to_usdt(bounty.token_name)
                    bounty.value_in_usdt = convert_amount(bounty.value_true, bounty.token_name, 'USDT')
                    bounty.value_in_usdt_now = bounty.value_in_usdt
                    bounty.value_in_eth = convert_amount(bounty.value_true, bounty.token_name, 'ETH')
                    bounty_increased = True
                except ConversionRateNotFoundError as e:
                    logger.debug(e)

            current_hours = int(bounty.estimated_hours)
            new_hours = int(params.get('hours')) if params.get('hours') else None
            if new_hours and current_hours != new_hours:
                bounty.estimated_hours = new_hours
                bounty.metadata['estimatedHours'] = new_hours
                bounty_changed = True

        if not bounty_changed and not bounty_increased:
            return JsonResponse({
                'success': True,
                'msg': _('Bounty details are unchanged.'),
                'url': bounty.absolute_url,
            })

        bounty.save()

        if bounty_changed:
            event = 'bounty_changed'
            record_bounty_activity(bounty, user, event)
            record_user_action(user, event, bounty)
            maybe_market_to_email(bounty, event)
        if bounty_increased:
            event = 'increased_bounty'
            record_bounty_activity(bounty, user, event)
            record_user_action(user, event, bounty)
            maybe_market_to_email(bounty, event)



        # notify a user that a bounty has been reserved for them
        if new_reservation and bounty.bounty_reserved_for_user:
            new_reserved_issue('founders@gitcoin.co', bounty.bounty_reserved_for_user, bounty)

        return JsonResponse({
            'success': True,
            'msg': _('You successfully changed bounty details.'),
            'url': bounty.absolute_url,
        })

    result = {}
    for key in keys:
        result[key] = getattr(bounty, key)
    del result['featuring_date']

    params = {
        'title': _('Change Bounty Details'),
        'pk': bounty.pk,
        'result': json.dumps(result),
        'is_bounties_network': bounty.is_bounties_network,
        'token_name': bounty.token_name,
        'token_address': bounty.token_address,
        'amount': bounty.get_value_true,
        'estimated_hours': bounty.estimated_hours
    }

    return TemplateResponse(request, 'bounty/change.html', params)


def get_users(request):
    token = request.GET.get('token', None)
    add_non_gitcoin_users = not request.GET.get('suppress_non_gitcoiners', None)

    if request.is_ajax():
        q = request.GET.get('term', '').lower()
        profiles = Profile.objects.filter(handle__startswith=q)
        results = []
        # try gitcoin
        for user in profiles:
            profile_json = {}
            profile_json['id'] = user.id
            profile_json['text'] = user.handle
            profile_json['avatar_url'] = user.avatar_url
            if user.avatar_baseavatar_related.filter(active=True).exists():
                profile_json['avatar_id'] = user.avatar_baseavatar_related.filter(active=True).first().pk
                profile_json['avatar_url'] = user.avatar_baseavatar_related.filter(active=True).first().avatar_url
            profile_json['preferred_payout_address'] = user.preferred_payout_address
            results.append(profile_json)
        # try github
        if not len(results) and add_non_gitcoin_users:
            search_results = search_users(q, token=token)
            for result in search_results:
                profile_json = {}
                profile_json['id'] = -1
                profile_json['text'] = result.login
                profile_json['email'] = None
                profile_json['avatar_id'] = None
                profile_json['avatar_url'] = result.avatar_url
                profile_json['preferred_payout_address'] = None
                # dont dupe github profiles and gitcoin profiles in user search
                if profile_json['text'].lower() not in [p['text'].lower() for p in profiles]:
                    results.append(profile_json)
        # just take users word for it
        if not len(results) and add_non_gitcoin_users:
            profile_json = {}
            profile_json['id'] = -1
            profile_json['text'] = q
            profile_json['email'] = None
            profile_json['avatar_id'] = None
            profile_json['preferred_payout_address'] = None
            results.append(profile_json)
        data = json.dumps(results)
    else:
        raise Http404
    mimetype = 'application/json'
    return HttpResponse(data, mimetype)


def get_kudos(request):
    autocomplete_kudos = {
        'copy': "No results found.  Try these categories: ",
        'autocomplete': ['rare','common','ninja','soft skills','programming']
    }
    if request.is_ajax():
        q = request.GET.get('term')
        network = request.GET.get('network', None)
        filter_by_address = request.GET.get('filter_by_address', '')
        eth_to_usd = convert_token_to_usdt('ETH')
        kudos_by_name = Token.objects.filter(name__icontains=q)
        kudos_by_desc = Token.objects.filter(description__icontains=q)
        kudos_by_tags = Token.objects.filter(tags__icontains=q)
        kudos_pks = (kudos_by_desc | kudos_by_name | kudos_by_tags).values_list('pk', flat=True)
        kudos = Token.objects.filter(pk__in=kudos_pks, hidden=False, num_clones_allowed__gt=0).order_by('name')
        if filter_by_address:
            kudos = kudos.filter(owner_address=filter_by_address)
        is_staff = request.user.is_staff if request.user.is_authenticated else False
        if not is_staff:
            kudos = kudos.filter(send_enabled_for_non_gitcoin_admins=True)
        if network:
            kudos = kudos.filter(contract__network=network)
        results = []
        for token in kudos:
            kudos_json = {}
            kudos_json['id'] = token.id
            kudos_json['token_id'] = token.token_id
            kudos_json['name'] = token.name
            kudos_json['name_human'] = humanize_name(token.name)
            kudos_json['description'] = token.description
            kudos_json['image'] = token.preview_img_url

            kudos_json['price_finney'] = token.price_finney / 1000
            kudos_json['price_usd'] = eth_to_usd * kudos_json['price_finney']
            kudos_json['price_usd_humanized'] = f"${round(kudos_json['price_usd'], 2)}"

            results.append(kudos_json)
        if not results:
            results = [autocomplete_kudos]
        data = json.dumps(results)
    else:
        raise Http404
    mimetype = 'application/json'
    return HttpResponse(data, mimetype)


def hackathon(request, hackathon='', panel='prizes'):
    """Handle rendering of HackathonEvents. Reuses the dashboard template."""

    try:
        hackathon_event = HackathonEvent.objects.filter(slug__iexact=hackathon).prefetch_related('sponsor_profiles').latest('id')
    except HackathonEvent.DoesNotExist:
        return redirect(reverse('get_hackathons'))

    title = hackathon_event.name.title()
    network = get_default_network()
    hackathon_not_started = timezone.now() < hackathon_event.start_date and not request.user.is_staff
    # if hackathon_not_started:
        # return redirect(reverse('hackathon_onboard', args=(hackathon_event.slug,)))

    orgs = []

    following_tribes = []

    if request.user and hasattr(request.user, 'profile'):
        following_tribes = request.user.profile.tribe_members.filter(
            org__in=hackathon_event.sponsor_profiles.all()
        ).values_list('org__handle', flat=True)

    for sponsor_profile in hackathon_event.sponsor_profiles.all():
        org = {
            'display_name': sponsor_profile.name,
            'avatar_url': sponsor_profile.avatar_url,
            'org_name': sponsor_profile.handle,
            'follower_count': sponsor_profile.tribe_members.all().count(),
            'followed': True if sponsor_profile.handle in following_tribes else False,
            'bounty_count': sponsor_profile.bounties.count()
        }
        orgs.append(org)

    if hasattr(request.user, 'profile') == False:
        is_registered = False
    else:
        is_registered = HackathonRegistration.objects.filter(registrant=request.user.profile,
                                                             hackathon=hackathon_event) if request.user and request.user.profile else None

    hacker_count = HackathonRegistration.objects.filter(hackathon=hackathon_event).all().count()
    projects_count = HackathonProject.objects.filter(hackathon=hackathon_event).all().count()
    view_tags = get_tags(request)
    active_tab = 0
    if panel == "prizes":
        active_tab = 0
    elif panel == "townsquare":
        active_tab = 1
    elif panel == "projects":
        active_tab = 2
    elif panel == "chat":
        active_tab = 3
    elif panel == "participants":
        active_tab = 4



    params = {
        'active': 'dashboard',
        'prize_count': hackathon_event.get_current_bounties.count(),
        'type': 'hackathon',
        'title': title,
        'target': f'/activity?what=hackathon:{hackathon_event.id}',
        'orgs': orgs,
        'keywords': json.dumps([str(key) for key in Keyword.objects.all().values_list('keyword', flat=True)]),
        'hackathon': hackathon_event,
        'hacker_count': hacker_count,
        'projects_count': projects_count,
        'hackathon_obj': HackathonEventSerializer(hackathon_event).data,
        'is_registered': json.dumps(True if is_registered else False),
        'hackathon_not_started': hackathon_not_started,
        'user': request.user,
        'avatar_url': request.build_absolute_uri(static('v2/images/twitter_cards/tw_cards-02.png')),
        'tags': view_tags,
        'activities': [],
        'SHOW_DRESSING': False,
        'use_pic_card': True,
        'projects': [],
        'panel': active_tab
    }

    if hackathon_event.identifier == 'grow-ethereum-2019':
        params['card_desc'] = "The ‘Grow Ethereum’ Hackathon runs from Jul 29, 2019 - Aug 15, 2019 and features over $10,000 in bounties"

    elif hackathon_event.identifier == 'beyondblockchain_2019':
        from dashboard.context.hackathon_explorer import beyondblockchain_2019
        params['sponsors'] = beyondblockchain_2019

    elif hackathon_event.identifier == 'eth_hack':
        from dashboard.context.hackathon_explorer import eth_hack
        params['sponsors'] = eth_hack

    params['keywords'] = programming_languages + programming_languages_full
    params['active'] = 'users'

    return TemplateResponse(request, 'dashboard/index-vue.html', params)


def hackathon_onboard(request, hackathon=''):
    referer = request.META.get('HTTP_REFERER', '')
    sponsors = {}
    is_registered = False
    try:
        hackathon_event = HackathonEvent.objects.filter(slug__iexact=hackathon).latest('id')
        hackathon_sponsors = HackathonSponsor.objects.filter(hackathon=hackathon_event)
        profile = request.user.profile if request.user.is_authenticated and hasattr(request.user, 'profile') else None
        is_registered = HackathonRegistration.objects.filter(registrant=profile, hackathon=hackathon_event) if profile else None

        if hackathon_sponsors:
            sponsors_gold = []
            sponsors_silver = []
            for hackathon_sponsor in hackathon_sponsors:
                sponsor = Sponsor.objects.get(name=hackathon_sponsor.sponsor)
                sponsor_obj = {
                    'name': sponsor.name,
                }
                if sponsor.tribe:
                    sponsor_obj['members'] = TribeMember.objects.filter(profile=sponsor.tribe).count()
                    sponsor_obj['handle'] = sponsor.tribe.handle

                if sponsor.logo_svg:
                    sponsor_obj['logo'] = sponsor.logo_svg.url
                elif sponsor.logo:
                    sponsor_obj['logo'] = sponsor.logo.url

                if hackathon_sponsor.sponsor_type == 'G':
                    sponsors_gold.append(sponsor_obj)
                else:
                    sponsors_silver.append(sponsor_obj)

            sponsors = {
                'sponsors_gold': sponsors_gold,
                'sponsors_silver': sponsors_silver
            }

    except HackathonEvent.DoesNotExist:
        hackathon_event = HackathonEvent.objects.last()

    params = {
        'active': 'hackathon_onboard',
        'title': f'{hackathon_event.name.title()} Onboard',
        'hackathon': hackathon_event,
        'referer': referer,
        'avatar_url': request.build_absolute_uri(static('v2/images/twitter_cards/tw_cards-02.png')),
        'is_registered': is_registered,
        'sponsors': sponsors,
        'onboard': True
    }

    if Poll.objects.filter(hackathon=hackathon_event).exists():
        params['poll'] = Poll.objects.filter(hackathon=hackathon_event).order_by('created_on').last()

    return TemplateResponse(request, 'dashboard/hackathon/onboard.html', params)


@csrf_exempt
@require_POST
def save_hackathon(request, hackathon):
    description = clean(
        request.POST.get('description'),
        tags=['a', 'abbr', 'acronym', 'b', 'blockquote', 'code', 'em', 'p', 's' 'u', 'br', 'i', 'li', 'ol', 'strong', 'ul', 'img', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'iframe', 'pre'],
        attributes={
            'a': ['href', 'title'],
            'abbr': ['title'],
            'acronym': ['title'],
            'img': ['src'],
            'iframe': ['src', 'frameborder', 'allowfullscreen'],
            '*': ['class', 'style']},
        styles=['background-color', 'color'],
        protocols=['http', 'https', 'mailto'],
        strip=True,
        strip_comments=True
    )

    if request.user.is_authenticated and request.user.is_staff:
        profile = request.user.profile if hasattr(request.user, 'profile') else None

        hackathon_event = HackathonEvent.objects.filter(slug__iexact=hackathon).latest('id')
        hackathon_event.description = description
        hackathon_event.save()
        return JsonResponse(
            {
                'success': True,
                'is_my_org': True,
            },
            status=200)
    else:
        return JsonResponse(
            {
                'success': False,
                'is_my_org': False,
            },
            status=401)


def hackathon_project(request, hackathon='', project=''):
    return hackathon_projects(request, hackathon, project)


def hackathon_projects(request, hackathon='', specify_project=''):
    q = clean(request.GET.get('q', ''), strip=True)
    order_by = clean(request.GET.get('order_by', '-created_on'), strip=True)
    filters = clean(request.GET.get('filters', ''), strip=True)
    sponsor = clean(request.GET.get('sponsor', ''), strip=True)
    page = request.GET.get('page', 1)

    try:
        hackathon_event = HackathonEvent.objects.filter(slug__iexact=hackathon).latest('id')
    except HackathonEvent.DoesNotExist:
        hackathon_event = HackathonEvent.objects.last()

    title = f'{hackathon_event.name.title()} Projects'
    desc = f"{title} in the recent Gitcoin Virtual Hackathon"
    avatar_url = hackathon_event.logo.url if hackathon_event.logo else request.build_absolute_uri(static('v2/images/twitter_cards/tw_cards-02.png'))

    if order_by not in {'created_on', '-created_on'}:
        order_by = '-created_on'

    projects = HackathonProject.objects.filter(hackathon=hackathon_event).exclude(status='invalid').prefetch_related('profiles').order_by(order_by).select_related('bounty')

    sponsors_list = []
    for project in projects:
        sponsor_item = {
            'avatar_url': project.bounty.avatar_url,
            'org_name': project.bounty.org_name
        }
        sponsors_list.append(sponsor_item)

    sponsors_list = list({v['org_name']:v for v in sponsors_list}.values())

    if q:
        projects = projects.filter(
            Q(name__icontains=q) |
            Q(summary__icontains=q) |
            Q(profiles__handle__icontains=q)
        )

    if sponsor:
        projects_sponsor=[]
        for project in projects:
            if sponsor == project.bounty.org_name:
                projects_sponsor.append(project)
        projects = projects_sponsor

    if filters == 'winners':
        projects = projects.filter(
            Q(badge__isnull=False)
        )
    if specify_project:
        projects = projects.filter(name__iexact=specify_project.replace('-', ' '))
        if projects.exists():
            project = projects.first()
            title = project.name
            desc = f'This project was created in the Gitcoin {hackathon_event.name.title()} Virtual Hackathon'
            if project.logo:
                avatar_url = project.logo.url


    projects_paginator = Paginator(projects, 9)

    try:
        projects_paginated = projects_paginator.page(page)
    except PageNotAnInteger:
        projects_paginated = projects_paginator.page(1)
    except EmptyPage:
        projects_paginated = projects_paginator.page(projects_paginator.num_pages)

    params = {
        'active': 'hackathon_onboard',
        'title': title,
        'card_desc': desc,
        'hackathon': hackathon_event,
        'sponsors_list': sponsors_list,
        'sponsor': sponsor,
        'avatar_url': avatar_url,
        'projects': projects_paginated,
        'order_by': order_by,
        'filters': filters,
        'query': q.split
    }

    return TemplateResponse(request, 'dashboard/hackathon/projects.html', params)


@csrf_exempt
def hackathon_get_project(request, bounty_id, project_id=None):
    profile = request.user.profile if request.user.is_authenticated and hasattr(request.user, 'profile') else None

    try:
        bounty = Bounty.objects.get(id=bounty_id)
        projects = HackathonProject.objects.filter(bounty__standard_bounties_id=bounty.standard_bounties_id, profiles__id=profile.id).nocache()
    except HackathonProject.DoesNotExist:
        pass

    if project_id:
        project_selected = projects.filter(id=project_id).first()
    else:
        project_selected = None

    params = {
        'bounty_id': bounty_id,
        'bounty': bounty,
        'projects': projects,
        'project_selected': project_selected,
        'avatar_url': request.build_absolute_uri(static('v2/images/twitter_cards/tw_cards-02.png')),
    }
    return TemplateResponse(request, 'dashboard/hackathon/project_new.html', params)


@csrf_exempt
@require_POST
def hackathon_save_project(request):

    project_id = request.POST.get('project_id')
    bounty_id = request.POST.get('bounty_id')
    profiles = request.POST.getlist('profiles[]')
    logo = request.FILES.get('logo')
    looking_members = request.POST.get('looking-members', '') == 'on'
    message = request.POST.get('looking-members-message', '')[:150]
    profile = request.user.profile if request.user.is_authenticated and hasattr(request.user, 'profile') else None
    error_response = invalid_file_response(logo, supported=['image/png', 'image/jpeg', 'image/jpg'])

    if error_response and error_response['status'] != 400:
        return JsonResponse(error_response)

    if profile is None:
        return JsonResponse({
            'success': False,
            'msg': '',
        })

    bounty_obj = Bounty.objects.get(pk=bounty_id)

    kwargs = {
        'name': clean(request.POST.get('name'),  strip=True),
        'hackathon': bounty_obj.event,
        'logo': request.FILES.get('logo'),
        'bounty': bounty_obj,
        'summary': clean(request.POST.get('summary'), strip=True),
        'work_url': clean(request.POST.get('work_url'), strip=True),
        'looking_members': looking_members,
        'message': message,
    }

    try:

        if profile.chat_id is '' or profile.chat_id is None:
            created, profile = associate_chat_to_profile(profile)
    except Exception as e:
        logger.info("Bounty Profile owner not apart of gitcoin")
    profiles_to_connect = [profile.chat_id]

    hackathon_admins = Profile.objects.filter(user__groups__name='hackathon-admin')

    try:
        for hack_admin in hackathon_admins:
            if hack_admin.chat_id is '' or hack_admin.chat_id is None:
                created, hack_admin = associate_chat_to_profile(hack_admin)
            profiles_to_connect.append(hack_admin.chat_id)
    except Exception as e:
        logger.debug('Error with adding admin')

    if project_id:
        try:

            project = HackathonProject.objects.filter(id=project_id, profiles__id=profile.id)

            kwargs.update({
                'logo': request.FILES.get('logo', project.first().logo)
            })

            project.update(**kwargs)

            try:
                bounty_profile = Profile.objects.get(handle=project.bounty.bounty_owner_github_username.lower())
                if bounty_profile.chat_id is '' or bounty_profile.chat_id is None:
                    created, bounty_profile = associate_chat_to_profile(bounty_profile)

                profiles_to_connect.append(bounty_profile.chat_id)
            except Exception as e:
                logger.info("Bounty Profile owner not apart of gitcoin")

            for profile_id in profiles:
                curr_profile = Profile.objects.get(id=profile_id)
                if not curr_profile.chat_id:
                    created, curr_profile = associate_chat_to_profile(curr_profile)
                profiles_to_connect.append(curr_profile.chat_id)

            add_to_channel.delay(project.first().chat_channel_id, profiles_to_connect)

            profiles.append(str(profile.id))
            project.first().profiles.set(profiles)

            invalidate_obj(project.first())

        except Exception as e:
            logger.error(f"error in record_action: {e}")
            return JsonResponse({'error': _('Error trying to save project')},
            status=401)
    else:

        try:
            project_channel_name = slugify(f'{kwargs["name"]}')

            created, channel_details = create_channel_if_not_exists({
                'team_id': settings.GITCOIN_HACK_CHAT_TEAM_ID,
                'channel_purpose': kwargs["summary"][:200],
                'channel_display_name': f'project-{project_channel_name}'[:60],
                'channel_name': project_channel_name[:60],
                'channel_type': 'P'
            })
            try:
                bounty_profile = Profile.objects.get(handle=bounty_obj.bounty_owner_github_username.lower())
                if bounty_profile.chat_id is '' or bounty_profile.chat_id is None:
                    created, bounty_profile = associate_chat_to_profile(bounty_profile)

                profiles_to_connect.append(bounty_profile.chat_id)
            except Exception as e:
                logger.info("Bounty Profile owner not apart of gitcoin")

            for profile_id in profiles:
                curr_profile = Profile.objects.get(id=profile_id)
                if not curr_profile.chat_id:
                    created, curr_profile = associate_chat_to_profile(curr_profile)
                profiles_to_connect.append(curr_profile.chat_id)

            add_to_channel.delay(channel_details, profiles_to_connect)

            kwargs.update({
                'chat_channel_id': channel_details['id']
            })
        except Exception as e:
            logger.error('Error creating project channel', e)

        project = HackathonProject.objects.create(**kwargs)
        project.save()
        profiles.append(str(profile.id))
        project.profiles.add(*list(filter(lambda profile_id: profile_id > 0, map(int, profiles))))

    return JsonResponse({
            'success': True,
            'msg': _('Project saved.')
        })


@csrf_exempt
@require_POST
def hackathon_registration(request):
    profile = request.user.profile if request.user.is_authenticated and hasattr(request.user, 'profile') else None

    hackathon = request.POST.get('name')
    referer = request.POST.get('referer')
    poll = request.POST.get('poll')
    email = request.user.email

    if not profile:
        return JsonResponse(
            {'error': _('You must be authenticated via github to use this feature!')},
            status=401)
    try:
        hackathon_event = HackathonEvent.objects.filter(slug__iexact=hackathon).latest('id')
        HackathonRegistration.objects.create(
            name=hackathon,
            hackathon=hackathon_event,
            referer=referer,
            registrant=profile
        )
        try:
            from chat.tasks import hackathon_chat_sync
            hackathon_chat_sync.delay(hackathon_event.id, profile.handle)
        except Exception as e:
            logger.error('Error while adding to chat', e)

        if poll:
            poll = json.loads(poll)
            set_questions = {}
            for entry in poll:
                question = get_object_or_404(Question, id=int(entry['name']))
                option = get_object_or_404(Option, id=int(entry['value']))

                Answer.objects.get_or_create(user=request.user, question=question, choice=option)

                values = set_questions.get(entry['name'], []) or []
                values.append(int(entry['value']))
                set_questions[entry['name']] = values

            for (question, choices) in set_questions.items():
                Answer.objects.exclude(user=request.user, question__id=int(question), choice__in=choices).delete()

    except Exception as e:
        logger.error('Error while saving registration', e)

    client = MailChimp(mc_api=settings.MAILCHIMP_API_KEY, mc_user=settings.MAILCHIMP_USER)
    mailchimp_data = {
            'email_address': email,
            'status_if_new': 'subscribed',
            'status': 'subscribed',

            'merge_fields': {
                'HANDLE': profile.handle,
                'HACKATHON': hackathon,
            },
        }

    user_email_hash = hashlib.md5(email.encode('utf')).hexdigest()

    try:
        client.lists.members.create_or_update(settings.MAILCHIMP_LIST_ID_HACKERS, user_email_hash, mailchimp_data)

        client.lists.members.tags.update(
            settings.MAILCHIMP_LIST_ID_HACKERS,
            user_email_hash,
            {
                'tags': [
                    {'name': hackathon, 'status': 'active'},
                ],
            }
        )
        print('pushed_to_list')
    except Exception as e:
        logger.error(f"error in record_action: {e}")
        pass

    if referer and '/issue/' in referer and is_safe_url(referer, request.get_host()):
        messages.success(request, _(f'You have successfully registered to {hackathon_event.name}. Happy hacking!'))
        redirect = referer
    else:
        messages.success(request, _(f'You have successfully registered to {hackathon_event.name}. Happy hacking!'))
        redirect = f'/hackathon/{hackathon}'

    return JsonResponse({'redirect': redirect})


def get_hackathons(request):
    """Handle rendering all Hackathons."""

    events = {
        'current': HackathonEvent.objects.current().filter(visible=True).order_by('start_date'),
        'upcoming': HackathonEvent.objects.upcoming().filter(visible=True).order_by('start_date'),
        'finished': HackathonEvent.objects.finished().filter(visible=True).order_by('-start_date'),
    }
    params = {
        'active': 'hackathons',
        'title': 'Hackathons',
        'avatar_url': request.build_absolute_uri(static('v2/images/twitter_cards/tw_cards-02.png')),
        'card_desc': "Gitcoin runs Virtual Hackathons. Learn, earn, and connect with the best hackers in the space -- only on Gitcoin.",
        'events': events,
    }
    return TemplateResponse(request, 'dashboard/hackathon/hackathons.html', params)


@login_required
def board(request):
    """Handle the board view."""

    user = request.user if request.user.is_authenticated else None
    keywords = user.profile.keywords

    context = {
        'is_outside': True,
        'active': 'dashboard',
        'title': 'Dashboard',
        'card_title': _('Dashboard'),
        'card_desc': _('Manage all your activity.'),
        'avatar_url': static('v2/images/helmet.png'),
        'keywords': keywords,
    }
    return TemplateResponse(request, 'board/index.html', context)


def funder_dashboard_bounty_info(request, bounty_id):
    """Per-bounty JSON data for the user dashboard"""

    user = request.user if request.user.is_authenticated else None
    if not user:
        return JsonResponse(
            {'error': _('You must be authenticated via github to use this feature!')},
            status=401)

    bounty = Bounty.objects.get(id=bounty_id)

    if bounty.status == 'open':
        interests = Interest.objects.prefetch_related('profile').filter(status='okay', bounty=bounty).all()
        profiles = [
            {'interest': {'id': i.id,
                          'issue_message': i.issue_message,
                          'pending': i.pending},
             'handle': i.profile.handle,
             'avatar_url': i.profile.avatar_url,
             'star_rating': i.profile.get_average_star_rating['overall'],
             'total_rating': i.profile.get_average_star_rating['total_rating'],
             'fulfilled_bounties': len(
                [b for b in i.profile.get_fulfilled_bounties()]),
             'leaderboard_rank': i.profile.get_contributor_leaderboard_index(),
             'id': i.profile.id} for i in interests]
    elif bounty.status == 'started':
        interests = Interest.objects.prefetch_related('profile').filter(status='okay', bounty=bounty).all()
        profiles = [
            {'interest': {'id': i.id,
                          'issue_message': i.issue_message,
                          'pending': i.pending},
             'handle': i.profile.handle,
             'avatar_url': i.profile.avatar_url,
             'star_rating': i.profile.get_average_star_rating()['overall'],
             'total_rating': i.profile.get_average_star_rating()['total_rating'],
             'fulfilled_bounties': len(
                [b for b in i.profile.get_fulfilled_bounties()]),
             'leaderboard_rank': i.profile.get_contributor_leaderboard_index(),
             'id': i.profile.id} for i in interests]
    elif bounty.status == 'submitted':
        fulfillments = bounty.fulfillments.prefetch_related('profile').all()
        profiles = []
        for f in fulfillments:
            profile = {'fulfiller_metadata': f.fulfiller_metadata, 'created_on': f.created_on}
            if f.profile:
                profile.update(
                    {'handle': f.profile.handle,
                     'avatar_url': f.profile.avatar_url,
                     'preferred_payout_address': f.profile.preferred_payout_address,
                     'id': f.profile.id})
            profiles.append(profile)
    else:
        profiles = []

    return JsonResponse({
                         'id': bounty.id,
                         'profiles': profiles})


def serialize_funder_dashboard_open_rows(bounties, interests):
    return [{'users_count': len([i for i in interests if b.pk in [i_b.pk for i_b in i.bounties]]),
             'title': b.title,
             'id': b.id,
             'standard_bounties_id': b.standard_bounties_id,
             'token_name': b.token_name,
             'value_in_token': b.value_in_token,
             'value_true': b.value_true,
             'value_in_usd': b.get_value_in_usdt,
             'github_url': b.github_url,
             'absolute_url': b.absolute_url,
             'avatar_url': b.avatar_url,
             'project_type': b.project_type,
             'expires_date': b.expires_date,
             'keywords': b.keywords,
             'interested_comment': b.interested_comment,
             'bounty_owner_github_username': b.bounty_owner_github_username,
             'submissions_comment': b.submissions_comment} for b in bounties]


def serialize_funder_dashboard_submitted_rows(bounties):
    return [{'users_count': b.fulfillments.count(),
             'title': b.title,
             'id': b.id,
             'token_name': b.token_name,
             'value_in_token': b.value_in_token,
             'value_true': b.value_true,
             'value_in_usd': b.get_value_in_usdt,
             'github_url': b.github_url,
             'absolute_url': b.absolute_url,
             'avatar_url': b.avatar_url,
             'project_type': b.project_type,
             'expires_date': b.expires_date,
             'interested_comment': b.interested_comment,
             'bounty_owner_github_username': b.bounty_owner_github_username,
             'submissions_comment': b.submissions_comment} for b in bounties]


def clean_dupe(data):
    result = []

    for d in data:
        if d not in result:
            result.append(d)
    return result


def funder_dashboard(request, bounty_type):
    """JSON data for the funder dashboard"""

    if not settings.DEBUG:
        network = 'mainnet'
    else:
        network = 'rinkeby'

    user = request.user if request.user.is_authenticated else None
    if not user:
        return JsonResponse(
            {'error': _('You must be authenticated via github to use this feature!')},
            status=401)

    profile = request.user.profile

    if bounty_type == 'open':
        bounties = list(Bounty.objects.filter(
            Q(idx_status='open') | Q(override_status='open'),
            current_bounty=True,
            network=network,
            bounty_owner_github_username__iexact=profile.handle,
            ).order_by('-interested__created', '-web3_created'))
        interests = list(Interest.objects.filter(
            bounty__pk__in=[b.pk for b in bounties],
            status='okay'))
        return JsonResponse(clean_dupe(serialize_funder_dashboard_open_rows(bounties, interests)), safe=False)

    elif bounty_type == 'started':
        bounties = list(Bounty.objects.filter(
            Q(idx_status='started') | Q(override_status='started'),
            current_bounty=True,
            network=network,
            bounty_owner_github_username__iexact=profile.handle,
            ).order_by('-interested__created', '-web3_created'))
        interests = list(Interest.objects.filter(
            bounty__pk__in=[b.pk for b in bounties],
            status='okay'))
        return JsonResponse(clean_dupe(serialize_funder_dashboard_open_rows(bounties, interests)), safe=False)

    elif bounty_type == 'submitted':
        bounties = Bounty.objects.prefetch_related('fulfillments').distinct('id').filter(
            Q(idx_status='submitted') | Q(override_status='submitted'),
            current_bounty=True,
            network=network,
            fulfillments__accepted=False,
            bounty_owner_github_username__iexact=profile.handle,
            )
        bounties.order_by('-fulfillments__created_on')
        return JsonResponse(serialize_funder_dashboard_submitted_rows(bounties), safe=False)

    elif bounty_type == 'expired':
        bounties = Bounty.objects.filter(
            Q(idx_status='expired') | Q(override_status='expired'),
            current_bounty=True,
            network=network,
            bounty_owner_github_username__iexact=profile.handle,
            ).order_by('-expires_date')

        return JsonResponse([{'title': b.title,
                              'token_name': b.token_name,
                              'value_in_token': b.value_in_token,
                              'value_true': b.value_true,
                              'value_in_usd': b.get_value_in_usdt,
                              'github_url': b.github_url,
                              'absolute_url': b.absolute_url,
                              'avatar_url': b.avatar_url,
                              'project_type': b.project_type,
                              'expires_date': b.expires_date,
                              'interested_comment': b.interested_comment,
                              'submissions_comment': b.submissions_comment}
                              for b in bounties], safe=False)



def contributor_dashboard(request, bounty_type):
    """JSON data for the contributor dashboard"""

    if not settings.DEBUG:
        network = 'mainnet'
    else:
        network = 'rinkeby'

    user = request.user if request.user.is_authenticated else None

    if not user:
        return JsonResponse(
            {'error': _('You must be authenticated via github to use this feature!')},
            status=401)

    profile = request.user.profile
    if bounty_type == 'work_in_progress':
        status = ['open', 'started']
        pending = False

    elif bounty_type == 'interested':
        status = ['open']
        pending = True

    elif bounty_type == 'work_submitted':
        status = ['submitted']
        pending = False

    if status:
        bounties = Bounty.objects.current().filter(
            interested__profile=profile,
            interested__status='okay',
            interested__pending=pending,
            idx_status__in=status,
            network=network,
            current_bounty=True).order_by('-interested__created')

        return JsonResponse([{'title': b.title,
                                'id': b.id,
                                'token_name': b.token_name,
                                'value_in_token': b.value_in_token,
                                'value_true': b.value_true,
                                'value_in_usd': b.get_value_in_usdt,
                                'github_url': b.github_url,
                                'absolute_url': b.absolute_url,
                                'avatar_url': b.avatar_url,
                                'project_type': b.project_type,
                                'expires_date': b.expires_date,
                                'interested_comment': b.interested_comment,
                                'submissions_comment': b.submissions_comment}
                                for b in bounties], safe=False)


@require_POST
@login_required
def change_user_profile_banner(request):
    """Handle Profile Banner Uploads"""

    filename = request.POST.get('banner')

    handle = request.user.profile.handle

    try:
        profile = profile_helper(handle, True)
        is_valid = request.user.profile.id == profile.id
        if filename[0:7] != '/static' or filename.split('/')[-1] not in load_files_in_directory('wallpapers'):
            is_valid = False
        if not is_valid:
            return JsonResponse(
                {'error': 'Bad request'},
                status=401)
        profile.profile_wallpaper = filename
        profile.save()
    except (ProfileNotFoundException, ProfileHiddenException):
        raise Http404

    response = {
        'status': 200,
        'message': 'User banner image has been updated.'
    }
    return JsonResponse(response)


@csrf_exempt
@require_POST
def choose_persona(request):

    if request.user.is_authenticated:
        profile = request.user.profile if hasattr(request.user, 'profile') else None
        if not profile:
            return JsonResponse(
                { 'error': _('You must be authenticated') },
                status=401
            )
        persona = request.POST.get('persona')
        if persona == 'persona_is_funder':
            profile.persona_is_funder = True
            profile.selected_persona = 'funder'
        elif persona == 'persona_is_hunter':
            profile.persona_is_hunter = True
            profile.selected_persona = 'hunter'
        profile.save()
    else:
        return JsonResponse(
            { 'error': _('You must be authenticated') },
            status=401
        )


    return JsonResponse(
        {
            'success': True,
            'persona': persona,
        },
        status=200
    )


def is_my_tribe_member(leader_profile, tribe_member):
    return any([tribe_member.org.handle.lower() == org.lower()
                for org in leader_profile.organizations])


@require_POST
def set_tribe_title(request):
    if request.user.is_authenticated:
        leader_profile = request.user.profile if hasattr(request.user,
                                                         'profile') else None
        member = request.POST.get('member')
        tribe_member = TribeMember.objects.get(pk=member)
        if not tribe_member:
            raise Http404
        if not is_my_tribe_member(leader_profile, tribe_member):
            return HttpResponse(status=403)
        tribe_member.title = request.POST.get('title')
        tribe_member.save()
        return JsonResponse({'success': True}, status=200)
    else:
        raise Http404


@csrf_exempt
@require_POST
def join_tribe(request, handle):
    if request.user.is_authenticated:
        profile = request.user.profile if hasattr(request.user, 'profile') else None
        try:
            try:
                TribeMember.objects.get(profile=profile, org__handle=handle.lower()).delete()
            except TribeMember.MultipleObjectsReturned:
                TribeMember.objects.filter(profile=profile, org__handle=handle.lower()).delete()

            return JsonResponse(
                {
                    'success': True,
                    'is_member': False,
                },
                status=200
            )
        except TribeMember.DoesNotExist:
            kwargs = {
                'org': Profile.objects.filter(handle=handle.lower()).first(),
                'profile': profile,
                'why': 'api',
            }
            tribemember = TribeMember.objects.create(**kwargs)
            tribemember.save()

            return JsonResponse(
                {
                    'success': True,
                    'is_member': True,
                },
                status=200
            )
    else:
        return JsonResponse(
            {
                'error': _('You must be authenticated via github to use this feature!')
            },
            status=401
        )




@csrf_exempt
@require_POST
def tribe_leader(request):
    if request.user.is_authenticated:
        member = request.POST.get('member')
        try:
            tribemember = TribeMember.objects.get(pk=member)
            is_my_org = request.user.is_authenticated and any([tribemember.org.handle.lower() == org.lower() for org in request.user.profile.organizations ])

            if is_my_org:
                tribemember.leader = True
                tribemember.save()
                return JsonResponse(
                    {
                        'success': True,
                        'is_leader': True,
                    },
                    status=200
                )
            else:
                return JsonResponse(
                    {
                        'success': False,
                        'is_my_org': False,
                    },
                    status=401
                )

        except Exception:

            return JsonResponse(
                {
                    'success': False,
                    'is_leader': False,
                },
                status=401
            )


@csrf_exempt
@require_POST
def save_tribe(request,handle):

    if not request.user.is_authenticated:
        return JsonResponse(
            {
                'success': False,
                'is_my_org': False,
                'message': 'user needs to be authenticated to use this'
            },
            status=405
        )

    try:
        is_my_org = request.user.is_authenticated and any([handle.lower() == org.lower() for org in request.user.profile.organizations ])
        if not is_my_org:
            return JsonResponse(
                {
                    'success': False,
                    'is_my_org': False,
                    'message': 'this operation is permitted to tribe owners only'
                },
                status=405
            )

        if request.POST.get('tribe_description'):

            tribe_description = clean(
                request.POST.get('tribe_description'),
                tags=['a', 'abbr', 'acronym', 'b', 'blockquote', 'code', 'em', 'p', 'u', 'br', 'i', 'li', 'ol', 'strong', 'ul', 'img', 'h1', 'h2'],
                attributes={'a': ['href', 'title'], 'abbr': ['title'], 'acronym': ['title'], 'img': ['src'], '*': ['class']},
                styles=[],
                protocols=['http', 'https', 'mailto'],
                strip=True,
                strip_comments=True
            )
            tribe = Profile.objects.filter(handle=handle.lower()).first()
            tribe.tribe_description = tribe_description
            tribe.save()

        if request.FILES.get('cover_image'):

            cover_image = request.FILES.get('cover_image', None)

            error_response = invalid_file_response(cover_image, supported=['image/png', 'image/jpeg', 'image/jpg'])

            if error_response:
                return JsonResponse(error_response)
            tribe = Profile.objects.filter(handle=handle.lower()).first()
            tribe.tribes_cover_image = cover_image
            tribe.save()

        if request.POST.get('tribe_priority'):

            tribe_priority = clean(
                request.POST.get('tribe_priority'),
                tags=['a', 'abbr', 'acronym', 'b', 'blockquote', 'code', 'em', 'p', 'u', 'br', 'i', 'li', 'ol', 'strong', 'ul', 'img', 'h1', 'h2'],
                attributes={'a': ['href', 'title'], 'abbr': ['title'], 'acronym': ['title'], 'img': ['src'], '*': ['class']},
                styles=[],
                protocols=['http', 'https', 'mailto'],
                strip=True,
                strip_comments=True
            )
            tribe = Profile.objects.filter(handle=handle.lower()).first()
            tribe.tribe_priority = tribe_priority
            tribe.save()

            if request.POST.get('publish_to_ts'):
                title = 'updated their priority to ' + request.POST.get('priority_html_text')
                kwargs = {
                    'profile': tribe,
                    'activity_type': 'status_update',
                    'metadata': {
                        'title': title,
                        'ask': '#announce'
                    }
                }
                activity = Activity.objects.create(**kwargs)
                wall_post_email(activity)

        return JsonResponse(
            {
                'success': True,
                'is_my_org': True,
            },
            status=200
        )

    except Exception:
        return JsonResponse(
            {
                'success': False,
                'is_leader': False,
                'message': 'something went wrong'
            },
            status=500
        )

@csrf_exempt
@require_POST
def create_bounty_v1(request):

    '''
        ETC-TODO
        - evaluate validity of duplicate / redundant data in models
        - wire in email (invite + successful creation)
    '''
    response = {
        'status': 400,
        'message': 'error: Bad Request. Unable to create bounty'
    }

    user = request.user if request.user.is_authenticated else None

    if not user:
        response['message'] = 'error: user needs to be authenticated to create bounty'
        return JsonResponse(response)

    profile = request.user.profile if hasattr(request.user, 'profile') else None

    if not profile:
        response['message'] = 'error: no matching profile found'
        return JsonResponse(response)

    if not request.method == 'POST':
        response['message'] = 'error: create bounty is a POST operation'
        return JsonResponse(response)

    github_url = request.POST.get("github_url", None)
    if Bounty.objects.filter(github_url=github_url).exists():
        response = {
            'status': 303,
            'message': 'bounty already exists for this github issue'
        }
        return JsonResponse(response)

    bounty = Bounty()

    bounty.bounty_owner_profile = profile
    bounty.bounty_state = 'open'
    bounty.title = request.POST.get("title", '')
    bounty.token_name = request.POST.get("token_name", '')
    bounty.bounty_type = request.POST.get("bounty_type", '')
    bounty.project_length = request.POST.get("project_length", '')
    bounty.estimated_hours = request.POST.get("estimated_hours")
    bounty.experience_level = request.POST.get("experience_level", '')
    bounty.github_url = github_url
    bounty.bounty_owner_github_username = request.POST.get("bounty_owner_github_username")
    bounty.is_open = True
    bounty.current_bounty = True
    bounty.issue_description = request.POST.get("issue_description", '')
    bounty.attached_job_description = request.POST.get("attached_job_description", '')
    bounty.fee_amount = request.POST.get("fee_amount")
    bounty.fee_tx_id = request.POST.get("fee_tx_id")
    bounty.metadata = json.loads(request.POST.get("metadata"))
    bounty.privacy_preferences = json.loads(request.POST.get("privacy_preferences", {}))
    bounty.funding_organisation = request.POST.get("funding_organisation")
    bounty.repo_type = request.POST.get("repo_type", 'public')
    bounty.project_type = request.POST.get("project_type", 'traditional')
    bounty.permission_type = request.POST.get("permission_type", 'permissionless')
    bounty.bounty_categories = request.POST.get("bounty_categories", '').split(',')
    bounty.network = request.POST.get("network", 'mainnet')
    bounty.admin_override_suspend_auto_approval = not request.POST.get("auto_approve_workers", True)
    bounty.value_in_token = request.POST.get("value_in_token", 0)
    bounty.token_address = request.POST.get("token_address")
    bounty.bounty_owner_email = request.POST.get("bounty_owner_email")
    bounty.bounty_owner_name = request.POST.get("bounty_owner_name", '') # ETC-TODO: REMOVE ?
    bounty.contract_address = bounty.token_address          # ETC-TODO: REMOVE ?
    bounty.balance = bounty.value_in_token                  # ETC-TODO: REMOVE ?
    bounty.raw_data = request.POST.get("raw_data", {})      # ETC-TODO: REMOVE ?
    bounty.web3_type = request.POST.get("web3_type", '')
    bounty.value_true = request.POST.get("amount", 0)
    bounty.bounty_owner_address = request.POST.get("bounty_owner_address", 0)

    current_time = timezone.now()

    bounty.web3_created = current_time
    bounty.last_remarketed = current_time

    try:
        bounty.token_value_in_usdt = convert_token_to_usdt(bounty.token_name)
        bounty.value_in_usdt = convert_amount(bounty.value_true, bounty.token_name, 'USDT')
        bounty.value_in_usdt_now = bounty.value_in_usdt
        bounty.value_in_eth = convert_amount(bounty.value_true, bounty.token_name, 'ETH')

    except ConversionRateNotFoundError as e:
        logger.debug(e)

    # bounty expiry date
    expires_date = int(request.POST.get("expires_date", 9999999999))
    bounty.expires_date = timezone.make_aware(
        timezone.datetime.fromtimestamp(expires_date),
        timezone=UTC
    )

    # bounty github data
    try:
        kwargs = get_url_dict(bounty.github_url)
        bounty.github_issue_details = get_gh_issue_details(**kwargs)
    except Exception as e:
        logger.error(e)

    # bounty is featured bounty
    bounty.is_featured = request.POST.get("is_featured", False)
    if bounty.is_featured:
        bounty.featuring_date = current_time

    # bounty is reserved for a user
    reserved_for_username = request.POST.get("bounty_reserved_for")
    if reserved_for_username:
        bounty.bounty_reserved_for_user = Profile.objects.get(handle=reserved_for_username.lower())
        if bounty.bounty_reserved_for_user:
            bounty.reserved_for_user_from = current_time
            release_to_public_after = request.POST.get("release_to_public")

            if release_to_public_after == "3-days":
                bounty.reserved_for_user_expiration = bounty.reserved_for_user_from + timezone.timedelta(days=3)
            elif release_to_public_after == "1-week":
                bounty.reserved_for_user_expiration = bounty.reserved_for_user_from + timezone.timedelta(weeks=1)

    # bounty is mapped to a hackathon
    event_tag = request.POST.get('eventTag')
    if event_tag:
        try:
            event = HackathonEvent.objects.filter(name__iexact=event_tag).latest('id')
            bounty.event = event
        except Exception as e:
            logger.error(e)

    # coupon code
    coupon_code = request.POST.get("coupon_code")
    try:
        if coupon_code:
            coupon = Coupon.objects.get(code=coupon_code)
            if coupon:
                bounty.coupon_code = coupon
    except Exception as e:
        logger.error(e)

    bounty.save()

    # save again so we have the primary key set and now we can set the
    # standard_bounties_id

    bounty.save()

    activity_ref = request.POST.get('activity', False)
    if activity_ref:
        try:
            comment = f'New Bounty created {bounty.get_absolute_url()}'
            activity_id = int(activity_ref)
            activity = Activity.objects.get(id=activity_id)
            activity.bounty = bounty
            activity.save()
            Comment.objects.create(profile=bounty.bounty_owner_profile, activity=activity, comment=comment)
        except (ValueError, Activity.DoesNotExist) as e:
            print(e)

    event_name = 'new_bounty'
    record_bounty_activity(bounty, user, event_name)
    maybe_market_to_email(bounty, event_name)

    # maybe_market_to_slack(bounty, event_name)
    # maybe_market_to_user_slack(bounty, event_name)

    response = {
        'status': 204,
        'message': 'bounty successfully created',
        'bounty_url': bounty.url
    }

    return JsonResponse(response)


@csrf_exempt
@require_POST
def cancel_bounty_v1(request):
    '''
        ETC-TODO
        - wire in email (invite + successful cancellation)
    '''
    response = {
        'status': 400,
        'message': 'error: Bad Request. Unable to cancel bounty'
    }

    user = request.user if request.user.is_authenticated else None

    if not user:
        response['message'] = 'error: user needs to be authenticated to cancel bounty'
        return JsonResponse(response)

    profile = request.user.profile if hasattr(request.user, 'profile') else None

    if not profile:
        response['message'] = 'error: no matching profile found'
        return JsonResponse(response)

    if not request.method == 'POST':
        response['message'] = 'error: cancel bounty is a POST operation'
        return JsonResponse(response)

    try:
       bounty = Bounty.objects.get(pk=request.POST.get('pk'))
    except Bounty.DoesNotExist:
        response['message'] = 'error: bounty not found'
        return JsonResponse(response)

    if bounty.bounty_state in ['cancelled', 'done']:
        response['message'] = 'error: bounty in ' + bounty.bounty_state + ' state cannot be cancelled'
        return JsonResponse(response)

    is_funder = bounty.is_funder(user.username.lower()) if user else False

    if not is_funder:
        response['message'] = 'error: bounty cancellation is bounty funder operation'
        return JsonResponse(response)

    canceled_bounty_reason = request.POST.get('canceled_bounty_reason')
    if not canceled_bounty_reason:
        response['message'] = 'error: missing canceled_bounty_reason'
        return JsonResponse(response)

    event_name = 'killed_bounty'
    record_bounty_activity(bounty, user, event_name)
    # maybe_market_to_email(bounty, event_name)
    # maybe_market_to_slack(bounty, event_name)
    # maybe_market_to_user_slack(bounty, event_name)

    bounty.bounty_state = 'cancelled'
    bounty.idx_status = 'cancelled'
    bounty.is_open = False
    bounty.canceled_on = timezone.now()
    bounty.canceled_bounty_reason = canceled_bounty_reason
    bounty.save()

    response = {
        'status': 204,
        'message': 'bounty successfully cancelled',
        'bounty_url': bounty.url
    }

    return JsonResponse(response)


@csrf_exempt
@require_POST
def fulfill_bounty_v1(request):
    '''
        ETC-TODO
        - wire in email (invite + successful fulfillment)
        - evalute BountyFulfillment unused fields
    '''
    response = {
        'status': 400,
        'message': 'error: Bad Request. Unable to fulfill bounty'
    }

    user = request.user if request.user.is_authenticated else None

    if not user:
        response['message'] = 'error: user needs to be authenticated to fulfill bounty'
        return JsonResponse(response)

    profile = request.user.profile if hasattr(request.user, 'profile') else None

    if not profile:
        response['message'] = 'error: no matching profile found'
        return JsonResponse(response)

    if not request.method == 'POST':
        response['message'] = 'error: fulfill bounty is a POST operation'
        return JsonResponse(response)

    try:
       bounty = Bounty.objects.get(github_url=request.POST.get('issueURL'))
    except Bounty.DoesNotExist:
        response['message'] = 'error: bounty not found'
        return JsonResponse(response)

    if bounty.bounty_state in ['cancelled', 'done']:
        response['message'] = 'error: bounty in ' + bounty.bounty_state + ' state cannot be fulfilled'
        return JsonResponse(response)

    if BountyFulfillment.objects.filter(bounty=bounty, profile=profile):
        response['message'] = 'error: user can submit once per bounty'
        return JsonResponse(response)

    fulfiller_address = request.POST.get('fulfiller_address')
    if not fulfiller_address:
        response['message'] = 'error: missing fulfiller_address'
        return JsonResponse(response)

    fulfiller_email = request.POST.get('email')
    if not fulfiller_email:
        response['message'] = 'error: missing email'
        return JsonResponse(response)

    hours_worked = request.POST.get('hoursWorked')
    if not hours_worked or not hours_worked.isdigit():
        response['message'] = 'error: missing hoursWorked'
        return JsonResponse(response)

    fulfiller_github_url = request.POST.get('githubPRLink')
    if not fulfiller_github_url:
        response['message'] = 'error: missing githubPRLink'
        return JsonResponse(response)

    event_name = 'work_submitted'
    record_bounty_activity(bounty, user, event_name)
    maybe_market_to_email(bounty, event_name)
    # maybe_market_to_slack(bounty, event_name)
    # maybe_market_to_user_slack(bounty, event_name)

    if bounty.bounty_state != 'work_submitted':
        bounty.bounty_state = 'work_submitted'
        bounty.idx_status = 'submitted'

    bounty.num_fulfillments += 1
    bounty.save()

    fulfillment = BountyFulfillment()

    fulfillment.bounty = bounty
    fulfillment.profile = profile

    now = timezone.now()
    fulfillment.created_on = now
    fulfillment.modified_on = now
    fulfillment.funder_last_notified_on = now
    fulfillment.token_name = bounty.token_name
    fulfillment.fulfiller_github_username = profile.handle

    # fulfillment.fulfiller_name    ETC-TODO: REMOVE ?
    # fulfillment.fulfillment_id    ETC-TODO: REMOVE ?

    fulfillment.fulfiller_address = fulfiller_address
    fulfillment.fulfiller_email = fulfiller_email
    fulfillment.fulfiller_hours_worked = hours_worked
    fulfillment.fulfiller_github_url = fulfiller_github_url

    fulfiller_metadata = request.POST.get('metadata', {})
    fulfillment.fulfiller_metadata = json.loads(fulfiller_metadata)

    fulfillment.save()

    response = {
        'status': 204,
        'message': 'bounty successfully fulfilled',
        'bounty_url': bounty.url
    }

    return JsonResponse(response)


@csrf_exempt
@require_POST
def payout_bounty_v1(request, fulfillment_id):
    '''
        ETC-TODO
        - wire in email (invite + successful payout)

        {
            amount: <integer>,
            bounty_owner_address : <char>,
            token_name : <char>
        }
    '''
    response = {
        'status': 400,
        'message': 'error: Bad Request. Unable to payout bounty'
    }

    user = request.user if request.user.is_authenticated else None

    if not user:
        response['message'] = 'error: user needs to be authenticated to fulfill bounty'
        return JsonResponse(response)

    profile = request.user.profile if hasattr(request.user, 'profile') else None

    if not profile:
        response['message'] = 'error: no matching profile found'
        return JsonResponse(response)

    if not request.method == 'POST':
        response['message'] = 'error: fulfill bounty is a POST operation'
        return JsonResponse(response)

    if not fulfillment_id:
        response['message'] = 'error: missing parameter fulfillment_id'
        return JsonResponse(response)

    try:
        fulfillment = BountyFulfillment.objects.get(pk=str(fulfillment_id))
        bounty = fulfillment.bounty
    except BountyFulfillment.DoesNotExist:
        response['message'] = 'error: bounty fulfillment not found'
        return JsonResponse(response)


    if bounty.bounty_state in ['cancelled', 'done']:
        response['message'] = 'error: bounty in ' + bounty.bounty_state + ' state cannot be paid out'
        return JsonResponse(response)

    is_funder = bounty.is_funder(user.username.lower()) if user else False

    if not is_funder:
        response['message'] = 'error: payout is bounty funder operation'
        return JsonResponse(response)

    if not bounty.bounty_owner_address:
        bounty_owner_address = request.POST.get('bounty_owner_address')
        if not bounty_owner_address:
            response['message'] = 'error: missing parameter bounty_owner_address'
            return JsonResponse(response)

        bounty.bounty_owner_address = bounty_owner_address
        bounty.save()

    amount = request.POST.get('amount')
    if not amount:
        response['message'] = 'error: missing parameter amount'
        return JsonResponse(response)

    token_name = request.POST.get('token_name')
    if not token_name:
        response['message'] = 'error: missing parameter token_name'
        return JsonResponse(response)

    payout_tx_id = request.POST.get('payout_tx_id')
    if payout_tx_id:
        fulfillment.payout_tx_id = payout_tx_id

    fulfillment.payout_amount = amount
    fulfillment.payout_status = 'pending'
    fulfillment.token_name = token_name
    fulfillment.save()

    sync_payout(fulfillment)

    response = {
        'status': 204,
        'message': 'bounty payment recorded. verification pending',
        'fulfillment_id': fulfillment_id
    }

    return JsonResponse(response)


@csrf_exempt
@require_POST
def close_bounty_v1(request, bounty_id):
    '''
        ETC-TODO
        - wire in email

    '''
    response = {
        'status': 400,
        'message': 'error: Bad Request. Unable to close bounty'
    }

    user = request.user if request.user.is_authenticated else None

    if not user:
        response['message'] = 'error: user needs to be authenticated to fulfill bounty'
        return JsonResponse(response)

    profile = request.user.profile if hasattr(request.user, 'profile') else None

    if not profile:
        response['message'] = 'error: no matching profile found'
        return JsonResponse(response)

    if not request.method == 'POST':
        response['message'] = 'error: close bounty is a POST operation'
        return JsonResponse(response)

    if not bounty_id:
        response['message'] = 'error: missing parameter bounty_id'
        return JsonResponse(response)

    try:
        bounty = Bounty.objects.get(pk=str(bounty_id))
        accepted_fulfillments = BountyFulfillment.objects.filter(bounty=bounty, accepted=True, payout_status='done')
    except Bounty.DoesNotExist:
        response['message'] = 'error: bounty not found'
        return JsonResponse(response)

    if bounty.bounty_state in ['cancelled', 'done']:
        response['message'] = 'error: bounty in ' + bounty.bounty_state + ' state cannot be closed'
        return JsonResponse(response)

    is_funder = bounty.is_funder(user.username.lower()) if user else False

    if not is_funder:
        response['message'] = 'error: closing a bounty funder operation'
        return JsonResponse(response)

    if accepted_fulfillments.count() == 0:
        response['message'] = 'error: cannot close a bounty without making a payment'
        return JsonResponse(response)

    event_name = 'work_done'
    record_bounty_activity(bounty, user, event_name)

    bounty.bounty_state = 'done'
    bounty.idx_status = 'done' # TODO: RETIRE
    bounty.is_open = False # TODO: fixup logic in status calculated property on bounty model
    bounty.accepted = True
    bounty.save()

    response = {
        'status': 204,
        'message': 'bounty is closed'
    }

    return JsonResponse(response)
