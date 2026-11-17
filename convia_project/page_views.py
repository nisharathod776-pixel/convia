from django.shortcuts import render


def landing_view(request):
    return render(request, "landing.html")


def login_view(request):
    return render(request, "login.html")


def register_view(request):
    return render(request, "register.html")


def dashboard_view(request):
    return render(request, "dashboard.html")


def meeting_room_view(request, meeting_code):
    return render(request, "meeting.html", {"meeting_code": meeting_code.lower()})


def profile_view(request):
    return render(request, "profile.html")


def admin_panel_view(request):
    return render(request, "admin.html")
