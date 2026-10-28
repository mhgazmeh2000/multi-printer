# -*- coding: utf-8 -*-
# =============================================================================
# @file:         tests/test_install_ps1.py
# @brief:        Tests for windows/install-snmp-exporter.ps1 (step G.4.3).
# @details:      Responsibilities: Check file encoding, PowerShell syntax and pinned versions.
#                Main API: 13 test functions
#                Side effects: runs external processes
# @dependencies: stdlib only
# @notes:        None.
# =============================================================================

"""
`windows/install-snmp-exporter.ps1` tests (step G.4.3).

In this sandbox there is no Windows, no pwsh, and no actual script execution or binary download
It is forbidden. So we have two layers:

  1) If `pwsh`/`powershell` is on the PATH: PowerShell's own official **parser check**
     (`System.Management.Automation.Language.Parser::ParseFile`) — no code execution;
  2) Always (so in CI and in this sandbox as well): textual check of elements that are not on the owner's server
     It either gives an installation error or quietly doesn't do what Phase G promises:
     version pin and URLs, bind address and firewall rules, log rotation, exit codes,
     Being idempotent, and BOM (reading Persian comments in Windows PowerShell 5.1).

No tests are skipped here (phase rule: the report should give "0 skipped") and nowhere
The script does not **execute**; Layer 1 measures the same text structure when not pwsh.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PS1 = ROOT / "windows" / "install-snmp-exporter.ps1"

NSSM_SETTINGS = ["AppParameters", "AppDirectory", "AppStdout", "AppStderr",
                 "AppRotateFiles", "AppRotateBytes", "Start"]
V3_ENV_VARS = ["SNMP_V3_USERNAME", "SNMP_V3_AUTH_PASSPHRASE", "SNMP_V3_PRIV_PASSPHRASE"]


def _src() -> str:
    """Full text, with help block (BOM removed)."""
    return PS1.read_text(encoding="utf-8-sig")


def _body() -> str:
    """The text of the file without the first `<# … #>` block — where the actual code lives."""
    return re.sub(r"^<#.*?#>\s*", "", _src(), count=1, flags=re.S)


def _code_only() -> str:
    """Without all comment lines — for structural checks."""
    return "\n".join(l for l in _body().splitlines() if not l.strip().startswith("#"))


def _shell():
    """Helper: shell."""
    return shutil.which("pwsh") or shutil.which("powershell") or shutil.which("powershell.exe")


# ── 1) The layer that requires PowerShell
def test_parser_check_with_powershell_or_structure_fallback():
    """Verifies parser check with powershell or structure fallback."""
    body, shell = _body(), _shell()
    if shell is None:
        # Not pwsh ⇒ Same thing as replaced in §5 of the document: Structure of healthy text
        assert body.count("{") == body.count("}"), "آکولادِ نامتوازن ⇒ ParserError"
        assert body.count('"') % 2 == 0, "تعدادِ \" فرد است ⇒ رشتهٔ بسته‌نشده"
        assert PS1.read_bytes()[:3] == b"\xef\xbb\xbf", "BOMِ لازمِ PowerShell 5.1 نیست"
        return
    cmd = ("$e=$null;$null=[System.Management.Automation.Language.Parser]::ParseFile("
           f"'{PS1.as_posix()}',[ref]$null,[ref]$e);"
           "if($e){($e|ForEach-Object{$_.Message})|Write-Host;exit 1}")
    proc = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", cmd],
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[:4000]


# ── 2) Decoding and file structure
def test_file_is_utf8_with_bom_and_no_crlf():
    """Verifies file is utf 8 with bom and no crlf."""
    raw = PS1.read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf", (
        "Windows PowerShell 5.1 بدونِ BOM فایلِ UTF-8 را با codepage سیستمی می‌خواند و "
        "کامنت‌ها/رشته‌های فارسی خراب می‌شوند")
    text = raw[3:].decode("utf-8")
    assert "\r" not in text, "در مخزن LF بماند؛ autocrlf در checkout خودش تبدیل می‌کند"
    assert text.endswith("\n")
    for marker in ("Ã", "â€", "\ufffd"):
        assert marker not in text, f"نشانهٔ کدگشاییِ غلط در فایل: {marker!r}"


def test_no_powershell7_only_syntax_and_braces_balance():
    """Verifies no powershell 7 only syntax and braces balance."""
    code = _code_only()
    for pattern, why in [(r"\?\?", "null-coalescing فقط PS7"),
                         (r"(?<![-\w])\|\|(?!\|)", "or-else فقط PS7"),
                         (r"(?<![-\w])&&", "and-then فقط PS7"),
                         (r"ForEach-Object\s+-Parallel", "-Parallel فقط PS7"),
                         (r"\$\w+\?\.", "null-conditional فقط PS7")]:
        assert not re.search(pattern, code), f"روی PowerShell 5.1 اجرا نمی‌شود: {why}"
    assert code.count("{") == code.count("}"), "آکولادِ نامتوازن ⇒ ParserError"
    assert code.count('"') % 2 == 0, "رشتهٔ بسته‌نشده"
    assert "Set-ItemProperty" not in code, (
        "Set-ItemProperty پارامتر -Type ندارد؛ MultiString فقط با New-ItemProperty نوشته می‌شود")


# ── 3) Pins and download sources
def test_pinned_versions_and_exact_release_urls():
    """Verifies pinned versions and exact release urls."""
    body = _body()
    assert re.search(r'^\$SNMP_EXPORTER_VERSION = "0\.26\.0"$', body, re.M), "پینِ نسخهٔ exporter عوض شده"
    assert re.search(r'^\$NSSM_VERSION\s+= "2\.24"$', body, re.M), "پینِ NSSM عوض شده"
    assert ("https://github.com/prometheus/snmp_exporter/releases/download/"
            "v$SNMP_EXPORTER_VERSION/$ExporterZip") in body
    assert 'snmp_exporter-$SNMP_EXPORTER_VERSION.windows-amd64.zip' in body
    assert "https://nssm.cc/release/$NssmZip" in body and 'nssm-$NSSM_VERSION.zip' in body
    # Only two external hosts: download sources. ``http://localhost'' is allowed because the probe
    # "Did the service come up?" and no download is made from it.
    hosts = set(re.findall(r"https?://([A-Za-z0-9.\-]+)", body))
    assert hosts == {"github.com", "nssm.cc", "localhost"}, f"میزبان‌های بیرونی: {sorted(hosts)}"
    for var in ("$ExporterUrl", "$NssmUrl"):
        line = re.search(rf'^\s*{re.escape(var)}\s+= "(https://[^"]+)"', body, re.M)
        assert line, f"{var} باید یک URLِ https باشد"


# ── 4) Network switch (decision G.4.2)
def test_switch_controls_listen_address_and_firewall_scope():
    """Verifies switch controls listen address and firewall scope."""
    body = _body()
    assert re.search(r"^\s*\[switch\]\$AllowSubnetAccess,$", body, re.M), "سوئیچ نیست"
    assert '$ListenAddress  = "127.0.0.1:9116"' in body, "پیش‌فرض باید localhost باشد"
    assert '$ListenAddress  = "0.0.0.0:9116"' in body, "حالت subnet تعریف نشده"
    assert '$FirewallRemote = @("127.0.0.1", "::1")' in body, (
        "-RemoteAddress آرایه می‌خواهد؛ رشتهٔ ویرگول‌دار برای cmdlet نامعتبر است")
    assert '$FirewallRemote = @("LocalSubnet")' in body, "حالت subnet برای فایروال تعریف نشده"
    assert "$FirewallRemoteText" in body, "نمایش/مقایسهٔ یکسانِ مقدارِ فایروال نیست"
    app_args = re.search(r"^\s*\$appArgs = .*$", body, re.M)
    assert app_args, "رشتهٔ آرگومان‌های سرویس ساخته نشده"
    for needle in ("--config.file=", "--web.listen-address=", "$ListenAddress", "--config.expand-env"):
        assert needle in app_args.group(0), f"AppParameters این را ندارد: {needle}"
    assert "-RemoteAddress $FirewallRemote" in body, "فایروال از سوئیچ پیروی نمی‌کند"
    assert '"WARN"' in body and "حالتِ پیش‌فرض (امن)" in body, "در هر دو حالت پیام چاپ نمی‌شود"


def test_localhost_is_the_default_branch_of_the_switch():
    """Verifies localhost is the default branch of the switch."""
    code = _code_only()
    at_switch = code.index("if ($AllowSubnetAccess)")
    at_subnet = code.index('"0.0.0.0:9116"')
    at_local = code.index('"127.0.0.1:9116"')
    assert at_switch < at_subnet < at_local, "ترتیبِ branchها بهم ریخته است"
    assert "} else {" in code[at_switch:at_local], "حالتِ پیش‌فرض باید در else باشد"


# 5) Admin, routes, exit codes
def test_admin_check_paths_and_exit_codes():
    """Verifies admin check paths and exit codes."""
    body = _body()
    assert "[Security.Principal.WindowsIdentity]::GetCurrent()" in body
    assert "WindowsPrincipal" in body and "WindowsBuiltInRole]::Administrator" in body
    assert "#Requires" not in body, "#Requires -RunAsAdministrator پیامِ خودِ ما را دور می‌زند"
    for pattern in [r'^\$InstallDir   = "C:\\PrintGuard\\snmp-exporter"$',
                    r'^\$DataDir      = "C:\\PrintGuard\\data"$',
                    r'^\$LogDir       = "C:\\PrintGuard\\logs"$',
                    r'^\$Port        = 9116$']:
        assert re.search(pattern, body, re.M), f"تنظیم نشده: {pattern}"
    assert re.search(r"^\s*exit 0\s*$", body, re.M), "پایانِ موفق باید exit 0 باشد"
    assert len(re.findall(r"Stop-WithCode 1 [\"(]", body)) >= 2, "خطای قابل‌توجه ⇒ exit 1 کم است"
    assert len(re.findall(r"Stop-WithCode 2 [\"(]", body)) >= 3, (
        "«اقدامِ دستی» باید exit 2 بگیرد: ادمین نبودن، نبودِ snmp.yml، ویندوز ۳۲بیتی، منبعِ ناشناس")
    assert '"exit=" + $Code' in body, "کدِ خروج در لاگ ثبت نمی‌شود"


# ── 6) Service and log rotation
def test_nssm_service_install_and_log_rotation():
    """Verifies nssm service install and log rotation."""
    body = _body()
    for verb in ("install", "set", "start"):
        assert f'Invoke-Nssm @("{verb}"' in body, f"دستور nssm «{verb}» صدا زده نمی‌شود"
    for setting in NSSM_SETTINGS:
        assert f'@("{setting}"' in body, f"nssm set {setting} تنظیم نشده"
    assert '"AppRotateFiles", "1"' in body, "چرخشِ لاگ روشن نشده"
    assert '"AppRotateBytes", "10485760"' in body, "سقفِ ۱۰MiB برای لاگ عدد ندارد"
    assert "SERVICE_AUTO_START" in body, "شروعِ خودکار ندارد"
    assert "snmp-exporter.log" in body and "snmp-exporter.err.log" in body, "مسیرِ stdout/stderr"
    assert "--config.file=" in body and "--config.expand-env" in body, (
        "بدون expand-env، placeholderهای ${SNMP_V3_*} خام می‌مانند")
    assert "nssm install printguard-snmp-exporter" in body, "معادلِ دستیِ nssm برای عیب‌یابی ثبت نشده"
    assert re.search(r'Expand-Archive -LiteralPath \$zip', body), "zip بازشده نمی‌شود"
    assert 'Get-ChildItem -LiteralPath $tmp -Recurse -Filter $ExeName' in body, "فایلِ هدف پیدا نمی‌شود"


def test_service_update_path_is_idempotent():
    """Verifies service update path is idempotent."""
    body = _body()
    assert re.search(r"\$svc = Get-Service -Name \$ServiceName -ErrorAction SilentlyContinue", body), (
        "وجودِ سرویس چک نمی‌شود")
    assert "به‌روزرسانیِ تنظیمات" in body, "مسیرِ update (بدون install دوباره) نیست"
    assert "function Stop-ServiceQuiet" in body, "قبلِ بازنویسیِ باینری سرویس متوقف نمی‌شود"
    assert "Get-Command nssm.exe" in body, "nssmِ موجود در PATH دیده نمی‌شود"
    # Four "safe to run again" paths: binary, nssm, config, firewall
    for phrase in ("از قبل نصب است ⇒ دانلود رد شد", "از قبل در مسیر نصب است ⇒ دانلود رد شد",
                   "عیناً همان فایلِ مخزن است ⇒ کپی نشد", "قاعدهٔ فایروال از قبل درست است"):
        assert phrase in body, f"مسیرِ idempotentِ «{phrase}» نیست"


# ── 7) Reservoir and firewall configuration
def test_installs_repo_config_without_touching_it_when_identical():
    """Verifies installs repo config without touching it when identical."""
    body = _body()
    assert re.search(r'\$SpecConfig   = Join-Path \$RepoRoot "monitoring\\snmp\.yml"', body), "منبعِ config"
    assert "Copy-Item -LiteralPath $SpecConfig -Destination $TargetConfig -Force" in body
    assert "Get-FileHash" in body, "مقایسهٔ hash برای skipِ بی‌دلیل انجام نمی‌شود"
    assert "عیناً همان فایلِ مخزن" in body, "پیامِ «تغییری ندارد» نیست"
    assert re.search(r"if \(-not \(Test-Path -LiteralPath \$SpecConfig\)\)", body), (
        "نبودِ snmp.yml باید exit 2 بگیرد، نه کپیِ شکست‌خورده")


def test_firewall_rule_shape():
    """Verifies firewall rule shape."""
    body = _body()
    m = re.search(r"New-NetFirewallRule[\s\S]{0,400}?\| Out-Null", body)
    assert m, "قاعدهٔ فایروال ساخته نمی‌شود"
    block = m.group(0).replace("`", " ")
    for needle in ("-DisplayName $DisplayName", "-Direction Inbound", "-LocalPort $Port",
                   "-Protocol TCP", "-Action Allow", "-RemoteAddress $FirewallRemote"):
        assert needle in block, f"فایروال این را ندارد: {needle}"
    assert "Get-NetFirewallRule" in body and "Remove-NetFirewallRule" in body, "قاعدهٔ کهنه مدیریت نمی‌شود"
    assert "Get-NetFirewallAddressFilter" in body and "Get-NetFirewallPortFilter" in body, (
        "شرطِ idempotencyِ قاعده سنجیده نمی‌شود")


# 8) Secrets
def test_v3_secrets_never_travel_on_the_command_line():
    """Verifies v 3 secrets never travel on the command line."""
    body = _body()
    for var in V3_ENV_VARS:
        assert var in body, f"متغیر {var} به سرویس منتقل نمی‌شود"
    assert not re.search(r'SNMP_V3_(AUTH_PASSPHRASE|PRIV_PASSPHRASE)\s*=\s*"[^"$][^"]*"', body), (
        "رمزِ hardcoded در اسکریپت است")
    assert 'Test-Path ("Env:" + $v)' in body, "مقادیر از محیطِ پروسه خوانده نمی‌شوند"
    assert "AppEnvironmentExtra" in body and "New-ItemProperty" in body, (
        "باید در registryِ سرویس نوشته شوند، نه argv (خطِ فرمانِ فرآیند همه‌جا دیده می‌شود)")
    assert '($_ -split "=", 2)[0]' in body, "در لاگ فقط نامِ متغیرها چاپ می‌شود؟"
    assert "استفاده‌ناپذیر" in body, "هشدارِ نبودِ SNMP_V3_* ثبت نشده"


def test_verification_and_next_step_messages():
    """Verifies verification and next step messages."""
    src, body = _src(), _body()
    assert "Invoke-WebRequest" in body and "StatusCode -eq 200" in body, "تأییدِ http انجام نمی‌شود"
    assert "Test-ServiceRunning" in body and 'Status -eq "Running"' in body, "وضعیتِ سرویس سنجیده نمی‌شود"
    assert "curl http://localhost:" in body and "module=printer_hp" in body, "نمونهٔ probe چاپ نمی‌شود"
    assert 'nssm remove " + $ServiceName + " confirm' in body, "راهنمای حذف نیست"
    assert "nssm remove printguard-snmp-exporter confirm" in src, "راهنمای حذف در help نیست"
    assert "$SNMP_EXPORTER_VERSION" in src.split("param(")[0], "راهنمای به‌روزرسانیِ نسخه در help نیست"
    assert "docs/windows-services.md" in src, "اشاره به سندِ مرحله نیست"
    for banned in ("Invoke-Expression", "Start-Process -Verb RunAs", "netsh ", "sc.exe delete"):
        assert banned not in src, f"این ساخته نباید در اسکریپت باشد: {banned}"
