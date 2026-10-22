from __future__ import annotations

from flask import Blueprint, jsonify, render_template
from flask_login import current_user
from config.settings import POLL_INTERVAL

bp = Blueprint("attendance", __name__, template_folder="templates")


@bp.route('/')
def index():
    username = current_user.username if current_user.is_authenticated else "میهمان"
    return render_template("attendance.html", poll_interval=POLL_INTERVAL, username=username)


@bp.route('/api/stats')
def api_stats():
    return jsonify({
        "present_count": 14,
        "on_leave_count": 2,
        "active_shift": "شیفت صبح (۰۸:۰۰ - ۱۶:۰۰)",
        "total_staff": 16,
        "recent_logs": [
            {"name": "علی رضایی", "time": "۰۸:۰۴", "status": "حاضر"},
            {"name": "محمد صادقی", "time": "۰۸:۱۵", "status": "حاضر"},
            {"name": "حسین مهدوی", "time": "—", "status": "مرخصی"},
        ]
    })
