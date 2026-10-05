from django.urls import path
from .views import MemberView, PgAvailabilityView, VerifyCheckPaymentView

urlpatterns = [
    path('members', MemberView.as_view(), name='member_api'),
    path('pg/availability', PgAvailabilityView.as_view(), name='pg_availability'),
    path('members/verify-check', VerifyCheckPaymentView.as_view(), name='verify_check_payment'),
]
