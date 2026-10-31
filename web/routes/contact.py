# =============================================================================
# @file:         web/routes/contact.py
# @brief:        Sample contact form protected by the self-hosted ALTCHA CAPTCHA.
# @details:      Responsibilities: show the contact form, validate the fields and the CAPTCHA
#                solution, and return a success message. The message is not stored or sent.
#                Main API: bp (blueprint), ContactForm
#                Side effects: none.
# @dependencies: internal: web.captcha | external: flask | templates: contact.html, _altcha_widget.html
# @notes:        This is a template for other forms. The route requires login (it is not in the
#                public endpoint list in web/__init__.py); add it there to make it public.
# =============================================================================

"""Sample contact form that shows how to use ALTCHA in a Flask form."""

from __future__ import annotations

from flask import Blueprint, render_template, request

from web.captcha import captcha_enabled, verify_captcha

bp = Blueprint("contact", __name__)


class ContactForm:
    """Holds the submitted contact fields and validates them, including the CAPTCHA.

    Attributes:
        email: Trimmed e-mail address entered by the user.
        message: Trimmed message body.
        remote_ip: Client address, passed on to the CAPTCHA check.
        errors: Human-readable validation errors (Persian, shown to the user).
    """

    def __init__(self, data, remote_ip: str | None = None) -> None:
        """Read the fields from a form-like mapping (for example ``request.form``)."""
        self._data = data
        self.email = (data.get("email") or "").strip()
        self.message = (data.get("message") or "").strip()
        self.remote_ip = remote_ip
        self.errors: list[str] = []

    def validate(self) -> bool:
        """Run all checks and collect the errors.

        Returns:
            bool: True when the form is valid and the CAPTCHA passed.
        """
        if "@" not in self.email:
            self.errors.append("ایمیل معتبر نیست")
        if not self.message:
            self.errors.append("متن پیام الزامی است")
        if not verify_captcha(self._data, self.remote_ip):
            self.errors.append("کپچا معتبر نیست")
        return not self.errors


@bp.route("/contact", methods=["GET", "POST"])
def contact():
    """Show the contact form (GET) or validate and acknowledge a submission (POST)."""
    form = ContactForm(request.form, request.remote_addr) if request.method == "POST" else None
    success = False
    if form is not None:
        success = form.validate()
    return render_template(
        "contact.html",
        form=form,
        success=success,
        captcha_enabled=captcha_enabled(),
    )
