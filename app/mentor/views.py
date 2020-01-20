# -*- coding: utf-8 -*-
"""Define view for the Mentor app.

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

"""

import json
import logging
from datetime import datetime

from dateutil import parser
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.staticfiles.templatetags.staticfiles import static
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_exempt

from dashboard.models import Profile
from .models import Sessions

logger = logging.getLogger(__name__)


def mentor_home(request):
    """Render the sessions home page."""
    listings = Sessions.objects.all()
    context = {
        'title': 'Mentor',
        "sessions": listings
    }
    return TemplateResponse(request, 'mentor_home.html', context)


@login_required
def join_session(request, session):
    """Render the sessions home page."""
    session = get_object_or_404(Sessions, id=session)
    context = {
        'title': 'Mentor',
        "session": session,
        'is_mentor': session.mentor.id == request.user.profile.id
    }
    return TemplateResponse(request, 'active_session.html', context)


@login_required
def new_session(request):
    """Render the sessions home page."""

    if request.method == "POST":
        name = request.POST.get('name', 'Mentoring')
        desc = request.POST.get('description', '')
        network = request.POST.get('network', 4)
        mentee = request.POST.get('mentee', None)
        try:
            price_per_min = request.POST.get('price_per_min')
        except ValueError:
            price_per_min = 1

        amount = 0
        mentee_profile = get_object_or_404(Profile, handle=mentee)
        session = Sessions(name=name,
                           description=desc,
                           priceFinney=amount,
                           network=network,
                           active=True,
                           price_per_min=price_per_min,
                           mentor=request.user.profile,
                           mentee=mentee_profile
                           )
        session.save()

        return redirect('session_join', session=session.id)

    listings = Sessions.objects.all()
    context = {
        'title': 'Mentor',
        "sessions": listings
    }
    return TemplateResponse(request, 'new_session.html', context)


@csrf_exempt
@login_required
def update_session(request, session):
    """Update current info of this set"""
    errors = []
    session = get_object_or_404(Sessions, id=session)
    name = request.POST.get('name', None)
    description = request.POST.get('description', None)
    metadata = request.POST.get('metadata', None)
    tags = request.POST.get('tags', None)
    active = request.POST.get('active', None)
    amount = request.POST.get('amount', None)
    tx_status = request.POST.get('tx_status', None)
    tx_id = request.POST.get('tx_id', None)
    tx_time = request.POST.get('tx_time', None)

    if name:
        session.name = name
        print('name')

    if description:
        session.description = description
        print('description')

    if metadata:
        try:
            session.metadata = json.loads(metadata)
            print(metadata)
        except json.decoder.JSONDecodeError:
            errors.append({'metadata': ['Error parsing json metadata']})

    if not active is None:
        print(active)
        session.active = active == 'true'

    if amount:
        try:
            session.amount = float(amount)
            print('amount')
        except ValueError:
            errors.append({'amount': ['Error parsing amount data']})

    if tx_status in ['na', 'pending', 'success''error', 'error', 'unknown', 'dropped']:
        session.tx_status = tx_status
        print('status')

    if not session.tx_id and tx_id:
        session.tx_received_on = datetime.now()
        try:
            session.tx_id = int(tx_id)
            print('tx')
        except ValueError:
            errors.append({'tx_id': ['Error parsing tx id']})

        try:
            session.tx_time = parser.parse(tx_time)
            print(tx_time)
        except ValueError:
            errors.append({'tx_time': ['Error parsing tx datetime']})

    if tags:
        try:
            session.tags = json.loads(tags)
            if not type(session.tags) is list:
                raise ValueError('Expected list')
        except (json.decoder.JSONDecodeError, ValueError):
            errors.append({'metadata': ['Error parsing tags data']})

    if errors:
        print('errors')
        return JsonResponse({
            'status': 'error',
            'message': 'Validation errors',
            'errors': errors
        }, status=422)

    session.save()
    print('save')
    return JsonResponse({'status': 'ok', 'message': ''})
