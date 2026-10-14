# Startup Radar

YC・a16z の YouTube 動画と、TechCrunch・Crunchbase News のスタートアップ記事を毎日自動で集め、
Claude に日本語で要約・評価させて、ブラウザで読めるようにするツール。

**デモ（実際の実行結果、閲覧のみ）**: <https://sssfcb1109-dev.github.io/ai-tools-portfolio/>

## なぜ作ったか

起業を検討していて、海外のスタートアップ情報を毎日追いたいが、英語の記事と1時間の動画を全部読む時間はない。
そこで「自分の事業にどれだけ関係あるか」で並べ替えた日本語の一覧が、毎朝できあがっている状態を作った。

## 処理の流れ

```
RSS（YC / a16z の YouTube、TechCrunch、Crunchbase News）
  ↓ 新着だけを取り出す（既読は seen.json で管理）
  ↓ 記事本文を取得（trafilatura）／動画は字幕を取得（タイムスタンプ付き）
  ↓ Claude Haiku   記事をまとめて一次評価：カテゴリ、日本語タイトル・要約、
  ↓                事業関連度★1〜5、トレンド重要度🔥1〜3
  ↓ Claude Sonnet  動画を要約し、重要な場面にタイムスタンプを付ける
  ↓ Claude Sonnet  ★4以上の記事だけ Web 検索で裏取りし、徹底分析
viewer.html（ローカルの閲覧画面）
```

- 毎日 3:00 に Windows のタスクスケジューラから実行する。
- Claude は API ではなく Claude Code の `claude -p`（ヘッドレスモード）で呼ぶ。サブスクの範囲で動くので、従量課金がかからない。
- 出力は `--json-schema` で型を固定し、パースの失敗をなくした。
- **評価の学習**：画面で★を直したりメモを書いたりすると、その修正例が次回の指示文に入り、AIの採点が自分の感覚に寄っていく。
- 事業の前提（「前提81」など番号付きの仮説）を指示文に渡しておくと、記事がどの前提を支持するか、覆すかを判定してバッジで出す。

## ファイル

| ファイル | 役割 |
|---|---|
| `radar.py` | 収集→評価→要約→深掘りの本体 |
| `serve.py` | 閲覧画面と評価修正の API（127.0.0.1 のみ） |
| `viewer.html` | 閲覧画面 |
| `prompts/` | 一次評価・動画要約・深掘りの指示文 |
| `config.json` | 情報源、モデルの使い分け、上限 |
| `context.example.md` | 読み手と事業の情報のひな形（実物は事業計画を含むため非公開） |

## 動かし方

```
pip install -r requirements.txt
copy context.example.md context.md   # 自分の事業に合わせて書き換える
python radar.py                      # 1回実行
python serve.py                      # http://127.0.0.1:8765/ で閲覧
```

Claude Code（`claude` コマンド）にログイン済みであることが前提。
