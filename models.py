# =============================================================================
# @file:         models.py
# @brief:        User model used by Flask-Login.
# @details:      Responsibilities: Represent a user record and its roles and permissions.
#                Main API: User
#                Side effects: none at import time
# @dependencies: internal: core.database | external: bcrypt, flask_login
# @notes:        None.
# =============================================================================

"""User model used by Flask-Login."""

from __future__ import annotations

import bcrypt
from flask_login import UserMixin

from core.database import (
    clear_password_reset_token,
    count_users,
    create_user,
    delete_user,
    get_user_by_email,
    get_user_by_google_id,
    get_user_by_id,
    get_user_by_identifier,
    get_user_by_reset_token_hash,
    list_users,
    set_password_reset_token,
    set_user_google_id,
    touch_user_login,
    update_user_access,
    update_user_role,
    update_user_verified,
    update_user_password,
)


class User(UserMixin):
    """Flask-Login user model backed by the users table."""

    def __init__(self, **kwargs):
        """Create a model instance from keyword fields, with defaults for missing ones."""
        self.id = kwargs.get("id")
        self.username = kwargs.get("username", "")
        self.email = kwargs.get("email", "")
        self.password_hash = kwargs.get("password_hash")
        self.google_id = kwargs.get("google_id")
        self.reset_token_hash = kwargs.get("reset_token_hash")
        self.reset_token_expires = kwargs.get("reset_token_expires")
        self.role = kwargs.get("role", "viewer")
        self.is_verified_flag = bool(kwargs.get("is_verified", kwargs.get("email_verified", False)))
        self.is_active_flag = bool(kwargs.get("is_active", True))
        self.created_at = kwargs.get("created_at")
        self.updated_at = kwargs.get("updated_at")
        self.last_login_at = kwargs.get("last_login_at")
        self.allowed_offices = kwargs.get("allowed_offices", []) or []
        self.allowed_modules = kwargs.get("allowed_modules", []) or []
        self.sensor_alert_enabled = bool(kwargs.get("sensor_alert_enabled", False))
        self.sensor_temp_warning = kwargs.get("sensor_temp_warning") or 30
        self.sensor_temp_critical = kwargs.get("sensor_temp_critical") or 35
        self.sensor_hum_warning_high = kwargs.get("sensor_hum_warning_high") or 70
        self.sensor_hum_critical_high = kwargs.get("sensor_hum_critical_high") or 80
        self.sensor_hum_warning_low = kwargs.get("sensor_hum_warning_low") or 20
        self.sensor_alert_cooldown_minutes = kwargs.get("sensor_alert_cooldown_minutes") or 30
        self.sensor_alert_start_hour = kwargs.get("sensor_alert_start_hour") or 0
        self.sensor_alert_end_hour = kwargs.get("sensor_alert_end_hour") or 24

    @property
    def is_active(self):
        """Return True when the account is active."""

        return self.is_active_flag

    @property
    def is_verified(self):
        """Return True when the account is verified."""

        return self.is_verified_flag

    @property
    def email_verified(self):
        """Return True when the e-mail address is verified."""

        return self.is_verified_flag

    @classmethod
    def from_dict(cls, data):
        """Build a User from a database row dict, or None for an empty row."""

        return cls(**data) if data else None

    @classmethod
    def get(cls, user_id):
        # Warning: This method is called on "class" (User.get(id) → Flask-Login).
        # On the examples sound user.get("role") — dictionary pattern — incorrectly to
        # The same classmethod arrives; With a clear message, we stand in front of the trap of time.
        """Load the user with this id, or None."""

        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            raise TypeError(
                f"User.get() سازنده‌ای کلاس‌متدی است و شناسه‌ی عددی کاربر می‌خواهد، "
                f"نه {user_id!r}. برای خواندن مقدار از نمونه‌ی User از صفت استفاده کنید "
                f"(مثلاً user.role به‌جای user.get('role'))."
            ) from None
        return cls.from_dict(get_user_by_id(uid))

    @classmethod
    def find_by_identifier(cls, identifier):
        """Find a user by username or e-mail address."""

        return cls.from_dict(get_user_by_identifier(identifier))

    @classmethod
    def find_by_email(cls, email):
        """Find a user by e-mail address."""

        return cls.from_dict(get_user_by_email(email))

    @classmethod
    def find_by_google_id(cls, google_id):
        """Find a user by Google account id."""

        return cls.from_dict(get_user_by_google_id(google_id))

    @classmethod
    def find_by_reset_token_hash(cls, token_hash):
        """Find the user that owns a password-reset token hash."""

        return cls.from_dict(get_user_by_reset_token_hash(token_hash))

    @classmethod
    def create(cls, username, email, password=None, google_id=None, role="viewer", is_verified=False, email_verified=None,
               allowed_offices=None, allowed_modules=None):
        """Create a user account and return the new User."""

        password_hash = None
        if password:
            password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        if email_verified is not None:
            is_verified = bool(email_verified)
        return cls.from_dict(
            create_user(
                username=username,
                email=email,
                password_hash=password_hash,
                google_id=google_id,
                role=role,
                is_verified=is_verified,
                allowed_offices=allowed_offices,
                allowed_modules=allowed_modules,
            )
        )

    @classmethod
    def all(cls):
        """Return all users."""

        return [cls.from_dict(row) for row in list_users()]

    @classmethod
    def total_count(cls):
        """Return the number of users."""

        return count_users()

    def verify_password(self, password: str) -> bool:
        """Check a plain-text password against the stored bcrypt hash."""

        if not self.password_hash or not password:
            return False
        try:
            return bcrypt.checkpw(password.encode("utf-8"), self.password_hash.encode("utf-8"))
        except Exception:
            return False

    def set_password(self, password: str) -> bool:
        """Hash and store a new password."""

        password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        ok = update_user_password(int(self.id), password_hash)
        if ok:
            self.password_hash = password_hash
        return ok

    def set_google_id(self, google_id: str) -> bool:
        """Link a Google account id to this user."""

        ok = set_user_google_id(int(self.id), google_id)
        if ok:
            self.google_id = google_id
        return ok

    def set_role(self, role: str) -> bool:
        """Change the role of this user."""

        ok = update_user_role(int(self.id), role)
        if ok:
            self.role = role
        return ok

    def set_access(self, allowed_offices=None, allowed_modules=None) -> bool:
        """Change the allowed offices and modules of this user."""

        ok = update_user_access(int(self.id), allowed_offices=allowed_offices, allowed_modules=allowed_modules)
        if ok:
            self.allowed_offices = list(allowed_offices or [])
            self.allowed_modules = list(allowed_modules or [])
        return ok

    def set_sensor_alert(self, enabled=False, temp_warning=30, temp_critical=35,
                         hum_warning_high=70, hum_critical_high=80, hum_warning_low=20,
                         cooldown_minutes=30, start_hour=0, end_hour=24) -> bool:
        """Store this user's sensor-alert settings."""

        from core.database import update_user_sensor_alert
        ok = update_user_sensor_alert(
            int(self.id), enabled=enabled,
            temp_warning=temp_warning, temp_critical=temp_critical,
            hum_warning_high=hum_warning_high, hum_critical_high=hum_critical_high,
            hum_warning_low=hum_warning_low,
            cooldown_minutes=cooldown_minutes,
            start_hour=start_hour, end_hour=end_hour,
        )
        if ok:
            self.sensor_alert_enabled = bool(enabled)
            self.sensor_temp_warning = temp_warning
            self.sensor_temp_critical = temp_critical
            self.sensor_hum_warning_high = hum_warning_high
            self.sensor_hum_critical_high = hum_critical_high
            self.sensor_hum_warning_low = hum_warning_low
            self.sensor_alert_cooldown_minutes = cooldown_minutes
            self.sensor_alert_start_hour = start_hour
            self.sensor_alert_end_hour = end_hour
        return ok

    def can_access_module(self, module_name: str) -> bool:
        """Return True when the user may use this module."""

        if self.role == "admin":
            return True
        module_name = str(module_name or "").strip().lower()
        allowed = [str(item).strip().lower() for item in (self.allowed_modules or []) if str(item).strip()]
        return not allowed or module_name in allowed

    def set_verified(self, is_verified: bool) -> bool:
        """Set the verified flag of this user."""

        ok = update_user_verified(int(self.id), is_verified)
        if ok:
            self.is_verified_flag = bool(is_verified)
        return ok

    def touch_login(self):
        """Record the current time as this user's last login."""

        touch_user_login(int(self.id))

    def set_reset_token(self, token_hash: str, expires_at: str) -> bool:
        """Store a hashed password-reset token and its expiry time."""

        return set_password_reset_token(int(self.id), token_hash, expires_at)

    def clear_reset_token(self) -> bool:
        """Remove this user's password-reset token."""

        ok = clear_password_reset_token(int(self.id))
        if ok:
            self.reset_token_hash = None
            self.reset_token_expires = None
        return ok

    def delete(self) -> bool:
        """Delete this user."""

        return delete_user(int(self.id))
