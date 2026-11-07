#!/usr/bin/env python3
"""يولّد ملفًا واحدًا `single_file.py` يجمع كل الأداة — للتشغيل من الجوال بلا مجلدات.

    python analysis/saudi_market/build_single_file.py

الملف الناتج لا يحتاج أي استيراد محلي: نزّله على الجوال وافتحه في Pydroid 3 واضغط Run،
أو شغّله في Termux. عدّل ثوابت الإعدادات في أعلى الملف الناتج فقط.

لا تعدّل `single_file.py` يدويًا — أي تعديل هنا أو في وحدات الأداة يجب أن يُتبَع بإعادة التوليد.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULES = ["saudi_data.py", "signals.py", "filters.py", "vector_engine.py", "run_backtest.py"]
LOCAL = r"(?:saudi_data|signals|filters|vector_engine|nautilus_engine)"
DROP = re.compile(
    rf"^(?:from {LOCAL} import|import {LOCAL}\b|from __future__ import annotations"
    rf"|sys\.path\.insert\(0, str\(Path\(__file__\)\.resolve\(\)\.parent\)\))"
)

REPLACEMENTS = {
    "from nautilus_engine import run_nautilus_backtest":
        'raise ImportError("نسخة الملف الواحد لا تضم محرك Nautilus — استخدم --mode vector")',
}
NAMESPACES = ["saudi_data", "signals", "filters", "vector_engine"]

HEADER = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""أداة اختبار «الشراء بعد أسابيع الهبوط» — ملف واحد للسوق السعودي.

الطريقة من الجوال (Pydroid 3):
    1) نزّل هذا الملف فقط.
    2) افتحه في Pydroid 3.
    3) عدّل الأسطر القريبة من الأعلى (SYMBOL و START).
    4) اضغط ▶.

الطريقة من الطرفية (Termux أو كمبيوتر):
    python single_file.py                    # يستخدم الإعدادات أدناه
    python single_file.py --demo --compare   # بيانات تجريبية بلا إنترنت
    python single_file.py --yahoo 1120.SR --start 2026-01-01 --compare
    python single_file.py --help             # كل الخيارات

ملف مُولَّد آليًا من saudi_data.py + signals.py + filters.py + vector_engine.py +
run_backtest.py بواسطة build_single_file.py. لا تعدّله يدويًا.
"""

from __future__ import annotations

'''

RUN_BLOCK = '''

# ---------------------------------------------------------------------------
# ٤) التشغيل
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) > 1:  # خيارات من الطرفية تتقدّم على الإعدادات أعلاه
        raise SystemExit(main(sys.argv[1:]))

    show_settings()
    try:
        code = main(build_args())
    except OSError as exc:  # no internet, DNS blocked, TLS rejected...
        print()
        print("تعذّر تحميل البيانات من الإنترنت:")
        print(f"  {exc}")
        print("الحل: تأكد من الاتصال، أو نزّل ملف CSV واكتب اسمه في CSV_FILE أعلى الملف.")
        raise SystemExit(1) from exc

    if code == 0 and SAVE_TRADES and TRADES_FILE.exists():
        print(f"تم حفظ الصفقات في: {TRADES_FILE}")
        print("(افتحه بـ Excel أو Google Sheets لترى كل صفقة)")

    raise SystemExit(code)
'''


def strip_module(path: Path) -> str:
    """يعيد نص الوحدة بعد حذف توثيق الملف والاستيرادات المحلية."""
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()

    # احذف توثيق الوحدة (module docstring) بدقة عبر ast
    skip: set[int] = set()
    tree = ast.parse(source)
    if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(
        tree.body[0].value, ast.Constant
    ):
        node = tree.body[0]
        skip.update(range(node.lineno - 1, node.end_lineno))

    # احذف كتلة التشغيل الخاصة بالوحدة (سنستعمل كتلة تشغيل الملف الواحد)
    for index, line in enumerate(lines):
        if line.startswith("if __name__ =="):
            lines = lines[:index]
            break

    kept: list[str] = []
    for index, line in enumerate(lines):
        if index in skip:
            continue
        if line.startswith("#!/usr/bin/env python3"):
            continue
        stripped = line.strip()
        if stripped in REPLACEMENTS:
            indent = line[: len(line) - len(line.lstrip())]
            kept.append(indent + REPLACEMENTS[stripped])
            continue
        if DROP.match(stripped) or DROP.match(line):
            continue
        kept.append(line)
    return "\n".join(kept).strip("\n")


def top_level_names(path: Path) -> list[str]:
    """أسماء المستوى الأعلى (دوال/أصناف/متغيرات) لبناء جسر الأسماء."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.append(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.append(node.target.id)
    return [n for n in names if not n.startswith("_")]


def settings_block() -> tuple[str, str]:
    """يعيد (ثوابت الإعدادات, دوال phone_run) من phone_run.py"""
    src = (HERE / "phone_run.py").read_text(encoding="utf-8")
    start = src.index('SYMBOL = "1120.SR"')
    end = src.index("# ---------------------------------------------------------------------------\n\nimport sys")
    constants = src[start:end].strip("\n")

    funcs_start = src.index("TRADES_FILE = HERE / \"trades.csv\"")
    funcs_end = src.index('if __name__ == "__main__":')
    funcs = src[funcs_start:funcs_end].strip("\n")
    funcs = funcs.replace(
        "HERE = Path(__file__).resolve().parent\nsys.path.insert(0, str(HERE))\n\n", ""
    )
    funcs = funcs.replace(
        'from run_backtest import main  # noqa: E402\n\n', ""
    )
    return constants, funcs


def build() -> Path:
    constants, funcs = settings_block()

    parts = [HEADER]
    parts.append(
        "# ===========================================================================\n"
        "# ١) الإعدادات — عدّل هنا فقط\n"
        "# ===========================================================================\n\n"
        f"{constants}\n\n"
    )
    parts.append(
        "# ===========================================================================\n"
        "# ٢) الاستيرادات (مكتبة بايثون القياسية فقط — لا حاجة لأي pip install)\n"
        "# ===========================================================================\n\n"
        "import argparse\nimport csv\nimport json\nimport random\nimport re\nimport sys\n"
        "import urllib.parse\nimport urllib.request\nfrom collections import deque\n"
        "from dataclasses import dataclass\nfrom dataclasses import field\n"
        "from datetime import date\nfrom datetime import datetime\nfrom datetime import timedelta\n"
        "from datetime import timezone\nfrom pathlib import Path\n"
        "from typing import Iterable\nfrom typing import Iterator\nfrom typing import Sequence\n\n"
    )

    for i, name in enumerate(MODULES, start=3):
        body = strip_module(HERE / name)
        title = f"# ٣) {name}" if i == 3 else ""
        parts.append(
            "\n# ===========================================================================\n"
            f"# من {name}\n"
            "# ===========================================================================\n\n"
            f"{body}\n"
        )

    bridge = [
        "\n# ===========================================================================\n",
        "# جسر الأسماء: الكود الأصلي يستدعي الوحدات بأسمائها (filters.x، vector_engine.y ...)\n",
        "# ===========================================================================\n\n",
        "import types as _types\n\n\n",
        "def _bridge(name, **members):\n",
        "    module = _types.ModuleType(name)\n",
        "    module.__dict__.update(members)\n",
        "    sys.modules.setdefault(name, module)\n",
        "    return module\n\n",
    ]
    for module in NAMESPACES:
        names = top_level_names(HERE / f"{module}.py")
        kwargs = ", ".join(f"{n}={n}" for n in names)
        bridge.append(f"{module} = _bridge(\"{module}\", {kwargs})\n")
    parts.append("".join(bridge))

    parts.append(
        "\n# ===========================================================================\n"
        "# ٣) دوال التشغيل المُبسّط (من phone_run.py)\n"
        "# ===========================================================================\n\n"
        "HERE = Path(__file__).resolve().parent\n\n"
        f"{funcs}\n"
    )
    parts.append(RUN_BLOCK)

    out = HERE / "single_file.py"
    out.write_text("".join(parts), encoding="utf-8")
    return out


if __name__ == "__main__":
    path = build()
    print(f"كتب {path.name}: {len(path.read_text(encoding='utf-8').splitlines())} سطرًا")
