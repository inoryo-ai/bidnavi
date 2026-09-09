"""要件IDのトレーサビリティ検査。

要件定義 付録B が「✅ 実装」と主張している要件について、
**その主張がコードから裏付けられるか**を機械的に確認する。

なぜ必要か:
  付録Bは自己申告である。読み手（採用選考者・将来の自分）が
  「FR-402 は本当に実装されているのか」を確かめる手段が
  全ファイルを読むことしかないと、**主張が検証不能**になる。
  要件IDをコードに書いておけば grep 一発で追える。

使い方:
  python scripts/check_traceability.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / 'docs' / 'requirements.md'
SRC = ROOT / 'src'
TESTS = ROOT / 'tests'

#: 付録Bの表から「✅」の行を拾うためのパターン
_APPENDIX_ROW = re.compile(r'^\|\s*((?:FR|NFR|CR|DQ|KPI)-[0-9A-Za-z]+)[^|]*\|\s*✅')

#: 要件IDらしき文字列
_REQ_ID = re.compile(r'\b(?:FR|NFR|CR|DQ|KPI)-[0-9A-Za-z]+\b')


def claimed_implemented() -> list[str]:
    """付録Bで「✅ 実装」と主張されている要件IDを集める。"""
    ids: list[str] = []
    for line in REQUIREMENTS.read_text(encoding='utf-8').splitlines():
        m = _APPENDIX_ROW.match(line)
        if m:
            ids.append(m.group(1))
    return ids


def ids_in(directory: Path) -> set[str]:
    found: set[str] = set()
    for path in directory.rglob('*.py'):
        if '__pycache__' in path.parts:
            continue
        found |= set(_REQ_ID.findall(path.read_text(encoding='utf-8')))
    return found


def main() -> int:
    claimed = claimed_implemented()
    if not claimed:
        print('付録Bから「✅ 実装」の行を1件も抽出できませんでした。'
              '表の形式が変わった可能性があります。', file=sys.stderr)
        return 1

    in_src = ids_in(SRC)
    in_tests = ids_in(TESTS)

    missing_src = [r for r in claimed if r not in in_src]
    missing_tests = [r for r in claimed if r not in in_tests]

    print(f'付録Bで「実装済み」と主張: {len(claimed)}件')
    print(f'  src に要件IDの記載あり  : {len(claimed) - len(missing_src)}件')
    print(f'  tests に要件IDの記載あり: {len(claimed) - len(missing_tests)}件')

    if missing_src:
        print('\n🔴 src から要件IDを辿れない（実装の有無ではなく、追跡可能性の問題）:')
        for r in missing_src:
            print(f'    {r}')

    if missing_tests:
        print('\n🟡 tests から要件IDを辿れない:')
        for r in missing_tests:
            print(f'    {r}')

    if missing_src:
        print('\n付録Bの主張をコードから検証できません。'
              '該当箇所の docstring に要件IDを記載してください。', file=sys.stderr)
        return 1

    print('\nトレーサビリティ: 付録Bの全主張が src から追跡可能')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
