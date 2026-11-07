"""修复从 E:\cursor_workspace\Vibe-Trading 迁移过来的 venv（旧路径 -> 新路径）"""
import configparser
import re
import sys
from pathlib import Path

OLD_BASE = r"E:\cursor_workspace\Vibe-Trading"
NEW_BASE = r"E:\Vibe-Trading"
OLD_PY = r"D:\ProgramFiles\Python313"
NEW_PY = r"C:\Users\Admin\.workbuddy\binaries\python\versions\3.13.12"

sp = Path(sys.prefix) / "Lib" / "site-packages"
scripts = Path(sys.prefix) / "Scripts"

# 1) 修复 editable .pth
for pth in sp.glob("__editable__*.pth"):
    txt = pth.read_text(encoding="utf-8", errors="replace")
    if OLD_BASE in txt or "cursor_workspace" in txt:
        fixed = txt.replace(OLD_BASE, NEW_BASE).replace(
            OLD_BASE.replace("\\", "\\\\"), NEW_BASE)
        pth.write_text(fixed, encoding="utf-8")
        print(f"[pth] fixed: {pth.name} -> {fixed.strip()}")

# 2) 修复激活脚本和纯文本脚本（activate、activate.bat、无扩展名脚本等）
for f in scripts.iterdir():
    if f.is_file() and f.suffix.lower() not in {".exe", ".pyc", ".pyd"}:
        try:
            txt = f.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, PermissionError):
            continue
        if "cursor_workspace" in txt or "ProgramFiles\\\\Python313" in txt or OLD_PY in txt:
            new = txt.replace(OLD_BASE, NEW_BASE).replace(OLD_PY, NEW_PY)
            new = new.replace(OLD_BASE.replace("\\", "\\\\"), NEW_BASE.replace("\\", "\\\\"))
            new = new.replace(OLD_PY.replace("\\", "\\\\"), NEW_PY.replace("\\", "\\\\"))
            f.write_text(new, encoding="utf-8")
            print(f"[script] fixed: {f.name}")

# 3) 用 distlib 重新生成所有 console_scripts exe 启动器
from pip._vendor.distlib.scripts import ScriptMaker

maker = ScriptMaker(None, str(scripts))
maker.executable = sys.executable
maker.clobber = True
maker.variants = {""}  # 只生成无 Python 版本后缀的启动器

count = 0
for ep_file in sorted(sp.glob("*.dist-info/entry_points.txt")):
    try:
        cp = configparser.ConfigParser()
        cp.optionxform = str
        cp.read(ep_file, encoding="utf-8")
    except Exception as e:
        print(f"[warn] skip {ep_file.parent.name}: {e}")
        continue
    for section in ("console_scripts", "gui_scripts"):
        for name, value in cp.items(section) if cp.has_section(section) else []:
            spec = f"{name} = {value}"
            try:
                maker.make(spec)
                count += 1
            except Exception as e:
                print(f"[warn] {name}: {e}")
print(f"[exe] regenerated {count} launchers")
