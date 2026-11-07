#!/usr/bin/env python3
"""تشغيل مبسّط من الجوال (Pydroid 3 أو Termux) — بدون أوامر أو وسائط.

الطريقة: افتح هذا الملف على جوالك، عدّل القيم في الأعلى، ثم اضغط Run.
لا يحتاج أي مكتبة خارجية (بايثون 3.10 أو أحدث فقط).

مثال على Termux:
    pkg install python git
    git clone https://github.com/aqaqpop1999-max/nautilus_trader.git
    cd nautilus_trader/analysis/saudi_market
    python phone_run.py

مثال على Pydroid 3:
    نزّل المشروع (Code -> Download ZIP) وفكّ الضغط، ثم افتح هذا الملف واضغط ▶.
"""

# ---------------------------------------------------------------------------
# ١) عدّل هنا فقط
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

# إذا لم يعمل الإنترنت: ضع اسم ملف CSV بجانب هذا الملف واكتبه هنا بدل ""
CSV_FILE = ""  # مثال: "1120_daily.csv"

# ---------------------------------------------------------------------------

import sys  # noqa: E402
from pathlib import Path  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from run_backtest import main  # noqa: E402

TRADES_FILE = HERE / "trades.csv"


def build_args() -> list[str]:
    argv: list[str] = []
    if CSV_FILE:
        argv.extend(["--csv", str(HERE / CSV_FILE), "--symbol", SYMBOL.split(".")[0]])
    else:
        argv.extend(["--yahoo", SYMBOL, "--symbol", SYMBOL.split(".")[0]])
    argv.extend(
        [
            "--start",
            START,
            "--history-start",
            HISTORY_START,
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
    )
    if COMPARE:
        argv.append("--compare")
    if SWEEP:
        argv.append("--sweep")
    if SAVE_TRADES:
        argv.extend(["--export-trades", str(TRADES_FILE)])
    return argv


def show_settings() -> None:
    source = f"ملف محلي: {CSV_FILE}" if CSV_FILE else f"Yahoo: {SYMBOL}"
    print("=" * 52)
    print(f"  المصدر      : {source}")
    print(f"  المتاجرة من : {START}   (بيانات من {HISTORY_START})")
    print(f"  القاعدة     : شراء بعد {N_DOWN} أسابيع هبوط، بيع بعد {HOLD_WEEKS} أسابيع")
    print(f"  الوقف       : {STOP_PCT}%   الهدف: {TARGET_PCT or 'بدون'}")
    print(f"  المقارنة    : {'نعم' if COMPARE else 'لا'}")
    print("=" * 52)
    print()


if __name__ == "__main__":
    show_settings()
    try:
        code = main(build_args())
    except OSError as exc:  # no internet, DNS blocked, TLS rejected...
        print()
        print("تعذّر تحميل البيانات من الإنترنت:")
        print(f"  {exc}")
        print("الحل: تأكد من الاتصال، أو نزّل ملف CSV يدويًا واكتب اسمه في CSV_FILE أعلى الملف.")
        raise SystemExit(1) from exc

    if code == 0 and SAVE_TRADES and TRADES_FILE.exists():
        print(f"تم حفظ الصفقات في: {TRADES_FILE}")
        print("(افتحه بـ Excel أو Google Sheets لترى كل صفقة)")

    raise SystemExit(code)
