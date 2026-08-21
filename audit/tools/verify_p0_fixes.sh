#!/bin/bash
# 复现三个 P0 的修复效果，把命令、退出码和输出全部落盘。
# 用法：bash audit/tools/verify_p0_fixes.sh
set -u
cd "$(dirname "$0")/../.." || exit 1
export PYTHONPATH=scripts:audit/tools
OUT=audit/evidence/after
SRC=audit/evidence/before/repro-paper.pdf
mkdir -p "$OUT"
: > "$OUT/exit-codes.txt"

record() { echo "$1=$2" >> "$OUT/exit-codes.txt"; }

# ---------- P0-1 ----------
rm -rf "$OUT/job-p0-1"
python3 scripts/init_job.py "$SRC" "$OUT/job-p0-1" --target-language zh-Hans --producer-id audit-verify > "$OUT/p0-1-init.txt" 2>&1
record p0-1-init $?
python3 - <<'PY'
import json, pathlib, sys
sys.path.insert(0, 'scripts')
from _common import load_json, write_json
p = pathlib.Path('audit/evidence/after/job-p0-1/translation.json')
d = load_json(p); d['terminology_reviewed'] = True; write_json(p, d)
PY
python3 scripts/plan_translation_batches.py "$OUT/job-p0-1" --model audit-verify-model > "$OUT/p0-1-plan.txt" 2>&1
record p0-1-plan $?
python3 - <<'PY'
import json, pathlib
b = json.load(open('audit/evidence/after/job-p0-1/translation-batches/batch-0001.json'))
pathlib.Path('audit/evidence/after/p0-1-results.json').write_text(
    json.dumps([{"id": u["id"], "translation": u["source"]} for u in b["units"]],
               ensure_ascii=False, indent=2), encoding='utf-8')
PY
python3 scripts/apply_translation_batch.py "$OUT/job-p0-1" --batch batch-0001 \
  --result "$OUT/p0-1-results.json" --model audit-verify-model > "$OUT/p0-1-apply.txt" 2>&1
record p0-1-apply $?

# ---------- P0-2 ----------
rm -rf "$OUT/job-p0-2"
python3 scripts/init_job.py "$SRC" "$OUT/job-p0-2" --target-language zh-Hans --producer-id audit-verify > /dev/null 2>&1
python3 - <<'PY'
import pathlib, sys
sys.path.insert(0, 'scripts')
from _common import load_json, write_json
p = pathlib.Path('audit/evidence/after/job-p0-2/translation.json')
d = load_json(p); d['terminology_reviewed'] = True; write_json(p, d)
PY
python3 scripts/plan_translation_batches.py "$OUT/job-p0-2" --model audit-verify-model > /dev/null 2>&1
python3 - <<'PY'
import json, pathlib
b = json.load(open('audit/evidence/after/job-p0-2/translation-batches/batch-0001.json'))
pathlib.Path('audit/evidence/after/p0-2-results.json').write_text(
    json.dumps([{"id": u["id"], "translation": None,
                 "keep_source_reason": "按学术规范保留原文"} for u in b["units"]],
               ensure_ascii=False, indent=2), encoding='utf-8')
pathlib.Path('audit/evidence/after/p0-2b-results.json').write_text(
    json.dumps([{"id": u["id"], "translation": None,
                 "keep_source_code": "bibliography-entry",
                 "keep_source_reason": "按学术规范保留原文"} for u in b["units"]],
               ensure_ascii=False, indent=2), encoding='utf-8')
PY
python3 scripts/apply_translation_batch.py "$OUT/job-p0-2" --batch batch-0001 \
  --result "$OUT/p0-2-results.json" --model audit-verify-model > "$OUT/p0-2-apply.txt" 2>&1
record p0-2-apply-freetext $?
python3 scripts/apply_translation_batch.py "$OUT/job-p0-2" --batch batch-0001 \
  --result "$OUT/p0-2b-results.json" --model audit-verify-model > "$OUT/p0-2b-apply.txt" 2>&1
record p0-2-apply-fake-bibliography $?
# 直接改文件绕过写入路径，验证完整性审查这一层也拦得住
python3 - <<'PY'
import pathlib, sys
sys.path.insert(0, 'scripts')
from _common import load_json, write_json
p = pathlib.Path('audit/evidence/after/job-p0-2/translation.json')
d = load_json(p)
for unit in d['units']:
    unit['translation'] = None
    unit['keep_source_code'] = None
    unit['keep_source_reason'] = '按学术规范保留原文'
d['coverage']['complete'] = True
write_json(p, d)
PY
python3 scripts/audit_translation_completeness.py "$OUT/job-p0-2" \
  --output-json "$OUT/p0-2-completeness.json" --output-md "$OUT/p0-2-completeness.md" > "$OUT/p0-2-completeness.txt" 2>&1
record p0-2-completeness $?

# ---------- P0-3 ----------
rm -rf "$OUT/job-p0-3"
python3 scripts/init_job.py "$SRC" "$OUT/job-p0-3" --target-language zh-Hans --producer-id audit-verify > "$OUT/p0-3-init.txt" 2>&1
record p0-3-init $?
python3 - <<'PY'
import pathlib, sys
sys.path.insert(0, 'scripts')
from _common import load_json, write_json
p = pathlib.Path('audit/evidence/after/job-p0-3/translation.json')
d = load_json(p); d['terminology_reviewed'] = True; write_json(p, d)
PY
python3 scripts/plan_translation_batches.py "$OUT/job-p0-3" --model audit-verify-model > /dev/null 2>&1
python3 - <<'PY'
import json, pathlib, sys
sys.path.insert(0, 'audit/tools')
from fake_translate import fake_translate
b = json.load(open('audit/evidence/after/job-p0-3/translation-batches/batch-0001.json'))
pathlib.Path('audit/evidence/after/p0-3-results.json').write_text(
    json.dumps([{"id": u["id"], "translation": fake_translate(u["source"])}
                for u in b["units"]], ensure_ascii=False, indent=2), encoding='utf-8')
PY
python3 scripts/apply_translation_batch.py "$OUT/job-p0-3" --batch batch-0001 \
  --result "$OUT/p0-3-results.json" --model audit-verify-model > "$OUT/p0-3-apply.txt" 2>&1
record p0-3-apply $?
python3 scripts/run_translation_batches.py "$OUT/job-p0-3" --verify-only > "$OUT/p0-3-verify.txt" 2>&1
record p0-3-batch-verify $?
python3 scripts/set_complex_content.py "$OUT/job-p0-3" --none --notes "合成测试论文，无复杂内容页" > /dev/null 2>&1
python3 - <<'PY'
import pathlib, sys
sys.path.insert(0, 'scripts')
from _common import load_json, write_json
root = pathlib.Path('audit/evidence/after/job-p0-3')
job = load_json(root / 'job.json')
job['route']['selected'] = job['route']['recommended']
job['route']['decision_reason'] = '合成测试论文，按推荐路线执行'
write_json(root / 'job.json', job)
inventory = load_json(root / 'figure_inventory.json')
inventory['inventory_complete'] = True
inventory['scope_note'] = '合成测试论文无图表'
write_json(root / 'figure_inventory.json', inventory)
PY
python3 scripts/validate_job.py "$OUT/job-p0-3" --stage translated --advance > "$OUT/p0-3-validate.txt" 2>&1
record p0-3-validate $?
# 关键：不手工修改 selected_fonts，只调用统一入口
python3 scripts/build_first_candidate.py "$OUT/job-p0-3" > "$OUT/p0-3-build.txt" 2>&1
record p0-3-build-first-candidate $?
python3 - <<'PY'
import json, pathlib
job = json.loads(pathlib.Path('audit/evidence/after/job-p0-3/job.json').read_text(encoding='utf-8'))
pathlib.Path('audit/evidence/after/p0-3-fonts.json').write_text(
    json.dumps({
        "selected_fonts": job['quality']['selected_fonts'],
        "selected_font_evidence": job['quality']['selected_font_evidence'],
    }, ensure_ascii=False, indent=2), encoding='utf-8')
PY
cat "$OUT/exit-codes.txt"
