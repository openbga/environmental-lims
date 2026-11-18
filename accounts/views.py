from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required
def password(request):
    """The signed-in person's password state; the change opens in the form dialog."""
    usable = request.user.has_usable_password()
    return render(
        request,
        "account/password_status.html",
        {"page_title": "Password", "usable_password": usable},
    )
