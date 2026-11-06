#!/usr/bin/env python3
"""تشغيل مبسّط من الجوال (Pydroid 3 أو Termux) — بدون أوامر أو وسائط.

الطريقة: افتح هذا الملف على جوالك، عدّل القيم في الأعلى، ثم اضغط Run.
لا يحتاج أي مكتبة خارجية (بايثون 3.10 أو أحدث فقط).

مثال على Termux:
    pkg install python git
    git clone https://github.com/aqaqpop1999-max/nautilus_trader.git
    cd nautilus_trader/analysis/saudi_market
    python phone_run.py
"""

# ---------------------------------------------------------------------------
# عدّل هنا فقط
# ---------------------------------------------------------------------------

SYMBOL = "1120.SR"  # 1120 الراجحي | 1180 الأهلي | 1010 الرياض | 2010 سابك | 7010 stc
START = "2026-01-01"  # بداية المتاجرة الفعلية
HISTORY_START = "2025-01-01"  # تاريخ إضافي مُبكر ليشتغل عليه متوسط 200 يوم
N_DOWN = 3  # عدد أسابيع الهبوط المتتالية المطلوبة
HOLD_WEEKS = 4  # مدة الاحتفاظ بالأسابيع
STOP_PCT = 7.0  # وقف الخسارة %
TARGET_PCT = 0.0  # هدف الربح % (0 = بدون هدف)
FEE_BPS = 15.5  # العمولة لكل جهة (نقطة أساس)

COMPARE = True  # جدول مقارنة الفلاتر
SWEEP = False  # مسح شبكي (بطيء على الجوال، لكن يعمل)
SAVE_TRADES = True  # حفظ الصفقات في ملف CSV بجانب هذا الملف

# ---------------------------------------------------------------------------

import glob  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_backtest import main  # noqa: E402


def build_args() -> list[str]:
    argv = [
        "--yahoo",
        SYMBOL,
        "--start",
        START,
        "--history-start",
        HISTORY_START,
        "--symbol",
        SYMBOL.split(".")[0],
        "--n-down",
        str(N_DOWN),
        "--hold-weeks",
        str(HOLD_WEEKS),
        "--stop",
        str(STOP_PCT),
        "--target",
        str(TARGET_PCT),
        "--fee-bps",
        str(FEE_BPS),
        "--mode",
        "vector",
    ]
    if COMPARE:
        argv.append("--compare")
    if SWEEP:
        argv.append("--sweep")
    if SAVE_TRADES:
        argv.extend(["--export-trades", "trades.csv"])
    return argv


if __name__ == "__main__":
    try:
        code = main(build_args())
    except OSError as exc:  # no internet, DNS blocked, TLS rejected...
        print("\nتعذّر تحميل البيانات من الإنترنت:", exc)
        print("تأكد من الاتصال، أو استخدم ملف CSV محلي بدل --yahoo.")
        raise SystemExit(1) from exc

    if SAVE_TRADES and glob.glob("trades.csv"):
        print("تم حفظ الصفقات في: trades.csv (افتحه بـ Excel أو Sheets)")

    raise SystemExit(code)
