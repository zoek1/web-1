from datetime import datetime
from django_filters import (BaseInFilter, NumberFilter,
                            CharFilter, FilterSet,
                            UUIDFilter, BooleanFilter, OrderingFilter)

from dashboard.models import Bounty


class CharInFilter(BaseInFilter, NumberFilter):
    pass


def filter_is_open(qs, name, value):
    lookup = {
        name: value == 'True',
        'expires_date__gt': datetime.now()
    }
    return qs.filter(**lookup)


class BountyFilter(FilterSet):
    pk = UUIDFilter(lookup_expr=['gt'])
    started = CharInFilter(field_name='interested__profile__handle')
    github_url = CharInFilter()
    fulfiller_github_username = CharFilter(field_name='fulfillments__fulfiller_github_username',
                                           lookup_expr=['iexact'])
    interested_github_username = CharFilter(field_name='interested__profile__handle',
                                            lookup_expr=['iexact'])
    is_open = BooleanFilter(method=filter_is_open)
    order_by = OrderingFilter(fields=('order_by', 'order_by'))

    class Meta:
        model = Bounty

