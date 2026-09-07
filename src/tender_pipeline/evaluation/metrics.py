"""精度の計測（要件定義 v2.0 §7.4 / KPI-1 / KPI-2 / AC-02）

このプロダクトでは **Recall（取りこぼさない）を Precision より優先する**。
1件の見逃しが顧客の受注機会喪失に直結するため、多少の誤検知は許容する。

したがってレポートの主指標は取りこぼし率（False Negative Rate）である。
F1 を主指標にしてはいけない。F1 は Precision と Recall を対等に扱うので、
「取りこぼしを増やして誤検知を減らす」改善を良い変更として評価してしまう。

**評価セットは実装者ではない立場（QA）が作る**（§7.4）。
作った本人が採点すると測定にならない。
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LabeledCase:
    """評価用の1件。`relevant` は人手で付けた正解ラベル。"""
    case_id: str
    company_id: str
    title: str
    relevant: bool
    price_text: str | None = None
    prefecture_code: str | None = None
    spec_text: str | None = None
    note: str = ''


@dataclass(frozen=True, slots=True)
class Prediction:
    case_id: str
    notified: bool
    #: ルール一致で通知が確定した、またはLLM（3段目）まで到達した
    #: ＝「AIが判断する機会を得た」か。
    had_a_chance: bool = True


@dataclass(frozen=True, slots=True)
class Reachability:
    """構造的な取りこぼしの計測（LLMの精度に依存しない指標）。

    関係のある案件が3段目（LLM）に到達すらしていない場合、
    それは**どれだけAIを良くしても拾えない**取りこぼしである。
    フィルタ設計そのものの欠陥なので、AIの精度とは分けて測る必要がある。

    技術検証フェーズではAPIキーが無くLLMの実精度を測れないため、
    設計の健全性はこの指標で判断する。
    """
    relevant_total: int
    relevant_with_chance: int

    @property
    def coverage(self) -> float:
        """関係ある案件のうち、判断の機会を得た割合。これが再現率の上限になる。"""
        return (self.relevant_with_chance / self.relevant_total
                if self.relevant_total else 1.0)

    @property
    def structural_miss_rate(self) -> float:
        """設計上どうやっても拾えない取りこぼしの率。"""
        return 1.0 - self.coverage


def reachability(
    cases: list[LabeledCase], predictions: list[Prediction]
) -> Reachability:
    chances = {p.case_id: p.had_a_chance for p in predictions}
    relevant = [c for c in cases if c.relevant]
    return Reachability(
        relevant_total=len(relevant),
        relevant_with_chance=sum(chances.get(c.case_id, False) for c in relevant),
    )


@dataclass(frozen=True, slots=True)
class Metrics:
    total: int
    true_positive: int
    false_positive: int
    true_negative: int
    false_negative: int

    @property
    def recall(self) -> float:
        """関係ある案件のうち、通知できた割合。KPI-1 の裏返し。"""
        denominator = self.true_positive + self.false_negative
        return self.true_positive / denominator if denominator else 1.0

    @property
    def miss_rate(self) -> float:
        """取りこぼし率（KPI-1）。**目標 3% 以下**。主指標。"""
        return 1.0 - self.recall

    @property
    def precision(self) -> float:
        """通知したうち、実際に関係があった割合。KPI-2。目標 70% 以上。"""
        denominator = self.true_positive + self.false_positive
        return self.true_positive / denominator if denominator else 1.0

    def meets_targets(self, *, max_miss_rate: float = 0.03,
                      min_precision: float = 0.70) -> bool:
        return self.miss_rate <= max_miss_rate and self.precision >= min_precision

    def report(self) -> str:
        lines = [
            f'件数            : {self.total}',
            f'取りこぼし率     : {self.miss_rate:.1%}  (KPI-1 目標 3.0% 以下) '
            f'{"OK" if self.miss_rate <= 0.03 else "NG"}',
            f'適合率           : {self.precision:.1%}  (KPI-2 目標 70% 以上) '
            f'{"OK" if self.precision >= 0.70 else "NG"}',
            f'再現率           : {self.recall:.1%}',
            f'内訳             : TP={self.true_positive} FP={self.false_positive} '
            f'TN={self.true_negative} FN={self.false_negative}',
        ]
        return '\n'.join(lines)


def score(cases: list[LabeledCase], predictions: list[Prediction]) -> Metrics:
    """正解ラベルと予測を突き合わせる。

    予測が欠けている case は「通知しなかった」とみなす。
    欠損を無視すると、パイプラインが落ちた分だけ成績が良く見える。
    """
    predicted = {p.case_id: p.notified for p in predictions}
    tp = fp = tn = fn = 0
    for case in cases:
        notified = predicted.get(case.case_id, False)
        if case.relevant and notified:
            tp += 1
        elif case.relevant and not notified:
            fn += 1
        elif not case.relevant and notified:
            fp += 1
        else:
            tn += 1
    return Metrics(len(cases), tp, fp, tn, fn)


def misses(cases: list[LabeledCase], predictions: list[Prediction]) -> list[LabeledCase]:
    """取りこぼした案件を返す。改善はここを1件ずつ潰すことでしか進まない。"""
    predicted = {p.case_id: p.notified for p in predictions}
    return [c for c in cases if c.relevant and not predicted.get(c.case_id, False)]


def load_cases(path: str | pathlib.Path) -> list[LabeledCase]:
    """JSONL の評価セットを読む。1行1件。"""
    cases: list[LabeledCase] = []
    for line in pathlib.Path(path).read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('//'):
            continue
        cases.append(LabeledCase(**json.loads(line)))
    _assert_unique(cases)
    return cases


def _assert_unique(cases: list[LabeledCase]) -> None:
    seen: set[str] = set()
    for c in cases:
        if c.case_id in seen:
            raise ValueError(f'case_id が重複しています: {c.case_id}')
        seen.add(c.case_id)
