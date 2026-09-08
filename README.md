# 入札案件情報パイプライン（技術検証フェーズ）

[![CI](https://github.com/inoryo-ai/public-tender-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/inoryo-ai/public-tender-pipeline/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![tests](https://img.shields.io/badge/tests-216%20passed-brightgreen)
![License](https://img.shields.io/badge/license-MIT-blue)

中小事業者が、自社に関係のある入札案件を「探さずに」受け取り、
応札するかどうかを当日中に判断できる状態をつくる。

### 3行で

1. **このプロダクトで最悪の事故は「クローラが落ちること」ではなく「エラーを出さずに静かに案件を失うこと」。**
   設計の大半はそれを検知するために書かれている（[10個の約束](#設計上絶対に崩してはいけない約束)）。
2. **実データで、監視では検知できないバグを2件見つけた**（文字化け・PDFの `(cid:NNN)`）。
   どちらも例外が出ず件数も正常に見える。
3. 🔴 **自分の設計仮説を実測で否定して、その数字ごと公開している**
   （[「埋め込みを入れれば解ける」は間違いだった](#2段目についての実測結果2026-09-07)）。
   **未達の項目も KPI を主張できない理由も、隠さず [§未達・要判断](#-未達要判断) に書いてある。**

- 要件定義: [`docs/requirements.md`](docs/requirements.md)（v3.0）
- 開発の経緯: 思考→計画→構築→レビュー→テスト→リリース→振り返りの7フェーズを5周

> 要件定義書に出てくる「桐島（CEO）」「天城（QA）」等は、1人＋AIで開発するために
> 用意した**役割ペルソナ**であり実在の人物ではありません。同じ成果物を視点を変えて
> 複数回レビューするための仕組みです。実装・判断はすべて単独で行っています。

**このリポジトリは事業判断を切り離した技術検証**であり、認証・課金・UI は作っていない。
検証しているのは「一番難しい部分」だけ（要件定義 §14）。

---

## 動かす

Python 3.11+。依存は `requests` / `beautifulsoup4` / `lxml` / `pdfplumber` /
`openpyxl` / `python-docx`（PDF・Excel・Word を扱わないなら前半3つだけで動く）。

```bash
pip install requests beautifulsoup4 lxml pdfplumber openpyxl python-docx pytest
export PYTHONPATH=src
```

### 通信せずに一通り試す

```bash
python -m tender_pipeline.cli init                       # DBを作る
python -m tender_pipeline.cli crawl --offline            # 保存済みHTMLで実行
python -m tender_pipeline.cli health                     # サイレント故障の判定
python -m tender_pipeline.cli match   --company co-demo --stub-llm
python -m tender_pipeline.cli funnel  --company co-demo  # 3段フィルタの通過率とコスト
python -m tender_pipeline.cli digest  --company co-demo  # 日次ダイジェスト
python -m tender_pipeline.cli evaluate tests/fixtures/eval_seed.jsonl --company co-demo --stub-llm
```

### 実サイトへクロールする

**問い合わせ先の設定が必須**。相手（自治体）が問題を感じたときに連絡できない
クローラは走らせない、という方針をコードで強制している（CR-102）。

```bash
cp .env.example .env          # TENDER_PIPELINE_CONTACT を自分の連絡先に書き換える
export TENDER_PIPELINE_CONTACT="you@example.com"
python -m tender_pipeline.cli crawl   # 1秒間隔・同時接続1・robots.txt 尊重
```

未設定のまま実行すると、通信せずに終了コード2で止まる。

### テスト

```bash
python -m pytest              # 216件（外部通信なし）
TENDER_PIPELINE_CONTACT="you@example.com" python -m pytest -m network   # 実サイト接続 1件
```

---

## 設計上、絶対に崩してはいけない約束

このプロダクトで最悪の事故は「クローラが落ちること」ではなく
**「エラーを出さずに、静かに案件を失うこと」** である。以下はそのための防御。

| # | 約束 | 実装 |
|---|------|------|
| 1 | 「取得成功(N件)」と「取得成功(0件)」を同一視しない | `CrawlStatus.OK` / `OK_EMPTY` を分離 |
| 2 | 一覧コンテナが消えたら 0件ではなく例外 | `SelectorMissError` |
| 3 | 例外を握りつぶさない。記録してから再送出する | `CrawlSession.__exit__` |
| 4 | 「無効なクローラ」を成功として扱わない | `CrawlStatus.DISABLED` |
| 5 | 時刻不明の締切を 00:00 で埋めない | `Deadline.time = None` + `TimeSource.ABSENT` |
| 6 | 「非公表」を 0円 にしない | `Price.undisclosed` + DB CHECK 制約 |
| 7 | 案件名が空のままキーを作らない | `EmptyTitleError` |
| 8 | AIが「関係なし」でも、キーワード一致なら通知する | `engine.evaluate` の `llm_overridden` |
| 9 | 予算上限に達しても通知は止めない | `BudgetExceeded` を捕まえて1・2段目で続行 |
| 10 | 件数上限で切った案件を黙って消さない | `Digest.omitted_count` |

### 入力は「相手のサイトが決めたもの」＝攻撃者が制御しうる前提で扱う

クローラは自治体サイトのHTML・PDF・Word/Excelを取り込み、その本文をLLMに渡す。
入力の中身をこちらは決められないので、攻撃入力を前提に防御する。

**実際に攻撃して再現させ、修正した結果**（`tests/test_security.py` に23件の回帰テスト）:

| # | 攻撃 | 実測した被害 | 対策 |
|---|------|------------|------|
| 1 | **ReDoS**：金額欄に長い数字列 | 🔴 **9万桁で 313秒 停止**（`(\d+)\s*億` が O(n²)）。案件1件でパイプライン全体が止まる | 桁数を16桁に固定＋数字列の先頭/末尾を `(?<!\d)` `(?!\d)` で締める → **0.0002秒** |
| 2 | **解凍爆弾**：docx/xlsx は実体がzip | 🔴 **51KB → 50MB（増幅率1,026倍）** を確認。上限が無くメモリ枯渇 | 受信バイト数に上限（既定25MB）。展開前に止める |
| 3 | **SSRF**：案件リンクに内部宛URL | `http://169.254.169.254/`（クラウドのメタデータ）等を取得しうる | スキーム許可制＋内部アドレス遮断。**リダイレクト後の最終URLも再検査** |
| 4 | **SQLインジェクション** | 誤検知だったが、列名が外部由来になれば成立する | 列名の許可リストを実装。外れたら `UnknownColumnError` |

補足:

- **桁溢れは切り詰めず「不明」にする。** 20桁の数字を16桁に切って取り込むと
  「1200兆円の案件」がDBに入り、金額レンジ絞り込みが壊れる。読めないものは読めないと記録する。
- **Content-Length は自己申告なので信じない。** 実体のバイト数でも必ず確認する。
- **添付ファイルを保存しない設計（FR-108）が効いている。** URLと抽出テキストしか持たないため、
  パストラバーサルが構造的に成立しない。

### 時刻不明の締切は、用途によって逆向きに倒す

```
通知     notify_at()          → 当日 9:00   （早い側。まだ間に合ううちに知らせる）
締切判定 deadline_expires_at() → 当日 23:59  （遅い側。当日の案件を昼に消さない）
```

**この非対称性は意図的。統一するとどちらかで案件を失う。**

---

## 構成

```
src/tender_pipeline/
  core/
    types.py         値オブジェクトと統制語彙（不変条件を型で強制）
    db.py            SQLite スキーマ（本番Postgresへ1対1で移植可能）
    html.py          文字コード事故の防止
    http.py          レート制限・robots.txt
    attachments.py   添付形式の線引きと抽出（§7.6）
    natural_key.py   案件の同一性判定（FR-106）
    normalize/       和暦・全角・元年・金額・案件名
  crawlers/
    base.py          ListingCrawler / SelectorMissError / レジストリ
    yokohama.py      横浜市（実データで動作確認済み）
  pipeline/
    run.py           冪等な実行ログ
    health.py        サイレント故障の検知
    ingest.py        正規化 → UPSERT → 訂正履歴
    lifecycle.py     締切判定・消えた案件の検出
  match/
    stages.py        1段目(ルール) / 2段目(類似度)
    llm.py           3段目。永続キャッシュ＋予算ガードレール
    engine.py        3段を束ねる。FR-305 の担保はここ
  evaluation/
    metrics.py       Recall優先の指標・構造的取りこぼしの計測
  notify/
    digest.py        日次ダイジェスト（通知疲れ対策）
```

---

## 現時点で分かっていること

### ✅ 検証できたこと

- **V-1 サイレント故障の検知**: 構造変化を注入すると `SelectorMissError` →
  `crawl_run.failed` → health アラート、まで一本でつながることを確認。
- **V-2 名寄せ**: 表記ゆれ（年度・括弧・定型句）を吸収し、
  枝番（その1/その2）と機関違いは分離。締切が延びても同一案件のまま。
- **実データで2件の重大バグを発見**（いずれも例外が出ず件数も正常で、監視で検知不能）
  - 横浜市が `charset` を返さないため `response.text` が文字化けする
  - 日本語PDFで ToUnicode マップが無いと `(cid:NNN)` の羅列が「成功」で返る

### ⚠️ 未達・要判断

| 項目 | 状態 |
|------|------|
| **V-3 LLMコストの実測** | **未実施**。`ANTHROPIC_API_KEY` が必要。要件 v3.0 §9.5 のゲートはこれが埋まるまで判定できない |
| **2段目（安価な事前フィルタ）** | **「埋め込みを入れれば解決する」は実測で否定された**。§9.2 でコスト設計を作り直した。下記参照 |
| **評価セット** | `tests/fixtures/eval_seed.jsonl` は30件の**合成シード**。要件が求める「実案件・QAが人手ラベル・dev/test分離」ではない。**この数字でKPI達成を主張してはならない** |
| **対象サイト** | 横浜市1件のみ。調達ポータル(GEPS)は**認証必須**で対象外と判明。ASP.NET/JS必須の枠は未着手 |

### 2段目についての実測結果（2026-09-07）

「文字バイグラムが弱いから埋め込みにすれば解ける」と考えたが、**測ったら違った。**

プロフィール「業務システム開発会社」に対する案件名の類似度:

| 案件名 | 正解 | bigram | e5-small |
|--------|------|--------|----------|
| 基幹業務システム改修業務委託 | ◯ | 0.586 | 0.849 |
| 庁内LAN更改に伴う設定業務 | ◯ | 0.105 | **0.856** |
| 庁舎警備業務委託 | × | 0.143 | **0.855** |
| 市道○○線舗装補修工事 | × | 0.000 | 0.821 |

- 関係あり群と無関係群の**分離幅は -0.001**（分布が重なっている）。絶対値の閾値が引けない。
- 評価30件での Recall@K も bigram とほぼ同じ。**Recall 100% には 22/30（73%）を3段目に回す必要**。
- 中心化・prefix 変更・過去実績案件を比較対象にする案、いずれも改善せず。

**結論**: 安価な事前フィルタで通過率を1%まで絞る、という前提を撤回した。
コスト制御は ①種目コード等の**構造化メタデータによるルール絞り込み**
②**LLMの2段構え**（安価モデルで一次判定→高スコアのみ上位モデルで要約）に移した。
埋め込みは「通過可否の判定」から外し「表示順の並び替え」に降格。

⚠️ 検証したのは `e5-small` のみ・合成30件のみ。
「埋め込みは使えない」ではなく「**使えるという前提で設計してはいけない**」が結論。
実案件と上位モデルでの再検証は要件 v3.0 の W0-5。
