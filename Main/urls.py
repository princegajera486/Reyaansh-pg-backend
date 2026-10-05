from django.contrib import admin
from django.urls import path, include
from django.http import JsonResponse

def health_check(request):
    return JsonResponse({'status': 'ok'})

urlpatterns = [
    path('', health_check, name='health_check_root'),
    path('admin/', admin.site.urls),
    path('ping/', health_check, name='health_check'),
    path('api/', include('DashBoard.urls')),
    path('api/', include('PgManagement.urls')),
    path('api/', include('PgMembers.urls')),
]
