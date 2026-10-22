"""
GitHub Data Source Driver (Phase J.7)
Fetches repository telemetry, metrics, commit logs, and issues from GitHub REST API.
"""

from __future__ import annotations

from typing import Any, Dict, List
import requests

from core.datasources.base import BaseDataSource, DataSourceError


class GitHubDataSource(BaseDataSource):
    type_name = "github"
    display_name = "گیت‌هاب (GitHub Metrics)"
    description = "پایش آمار مخازن کد، رخدادهای کامیت و وضعیت ایشوهای پروژه"
    icon = "github"

    @classmethod
    def config_schema(cls) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "default": "printguard"},
                "repo": {"type": "string", "default": "multi-printer"},
                "token": {"type": "string"},
                "timeout": {"type": "integer", "default": 10},
            },
            "required": ["owner", "repo"],
        }

    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "PrintGuard-Observability-Agent",
        }
        token = self.config.get("token")
        if token:
            headers["Authorization"] = f"token {token}"
        return headers

    def test_connection(self) -> Dict[str, Any]:
        owner = self.config.get("owner")
        repo = self.config.get("repo")
        if not owner or not repo:
            raise DataSourceError("مشخص کردن owner و repo الزامی است.")

        url = f"https://api.github.com/repos/{owner}/{repo}"
        headers = self._get_headers()
        timeout = self.config.get("timeout", 10)

        try:
            res = requests.get(url, headers=headers, timeout=timeout)
            if res.status_code == 200:
                data = res.json()
                stars = data.get("stargazers_count", 0)
                forks = data.get("forks_count", 0)
                return {
                    "status": "ok",
                    "message": f"اتصال به مخزن {owner}/{repo} برقرار شد (ستاره‌ها: {stars}، فورک‌ها: {forks}).",
                }
            elif res.status_code == 404:
                raise DataSourceError(f"مخزن {owner}/{repo} یافت نشد (یا دسترسی عمومی ندارد).")
            raise DataSourceError(f"پاسخ خطای گیت‌هاب: HTTP {res.status_code}")
        except Exception as e:
            raise DataSourceError(f"خطا در اتصال به API گیت‌هاب: {e}")

    def query(self, query_params: Dict[str, Any]) -> Dict[str, Any]:
        owner = self.config.get("owner")
        repo = self.config.get("repo")
        metric_type = query_params.get("metric_type", "overview")
        headers = self._get_headers()
        timeout = self.config.get("timeout", 10)

        base = f"https://api.github.com/repos/{owner}/{repo}"

        try:
            if metric_type == "overview":
                res = requests.get(base, headers=headers, timeout=timeout)
                res.raise_for_status()
                d = res.json()
                rows = [
                    {
                        "repo": f"{owner}/{repo}",
                        "stars": d.get("stargazers_count", 0),
                        "forks": d.get("forks_count", 0),
                        "open_issues": d.get("open_issues_count", 0),
                        "subscribers": d.get("subscribers_count", 0),
                        "default_branch": d.get("default_branch", "main"),
                    }
                ]
                columns = [
                    {"field": "repo", "title": "مخزن", "type": "text"},
                    {"field": "stars", "title": "ستاره‌ها", "type": "number"},
                    {"field": "forks", "title": "فورک‌ها", "type": "number"},
                    {"field": "open_issues", "title": "ایشوهای باز", "type": "number"},
                ]
                return {"columns": columns, "rows": rows, "total": 1}

            elif metric_type == "commits":
                res = requests.get(f"{base}/commits", headers=headers, params={"per_page": 20}, timeout=timeout)
                res.raise_for_status()
                commits = res.json()
                rows = []
                for c in commits:
                    commit_obj = c.get("commit", {})
                    author_obj = commit_obj.get("author", {})
                    rows.append({
                        "sha": c.get("sha", "")[:7],
                        "author": author_obj.get("name", "Unknown"),
                        "date": author_obj.get("date", ""),
                        "message": commit_obj.get("message", "").split("\n")[0],
                    })
                columns = [
                    {"field": "sha", "title": "Commit", "type": "text"},
                    {"field": "author", "title": "نویسنده", "type": "text"},
                    {"field": "date", "title": "تاریخ", "type": "date"},
                    {"field": "message", "title": "توضیح کامیت", "type": "text"},
                ]
                return {"columns": columns, "rows": rows, "total": len(rows)}

            else:
                raise DataSourceError(f"نوع شاخص پشتیبانی‌نشده: {metric_type}")

        except Exception as e:
            raise DataSourceError(f"خطا در دریافت اطلاعات از گیت‌هاب: {e}")
