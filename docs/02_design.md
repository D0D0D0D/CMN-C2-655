# Template Design Specification — CMN-C2-655 CorporateGroupTripPlannerAgent

> This document describes the template's architecture and operating constraints.

## Position in AgentCore Architecture

- **Agent Class**: `CorporateGroupTripPlannerGraph`
- **L1 Base**: **AgentBaseGraph**（L1 直継承。main スロットに `GraphNode` サブクラスを置く Cat 2 標準構成）
- **Three-Layer Separation**:
  - State: flat TypedDict composition（Pydantic 禁止 — msgpack 非互換）
  - Node: L1 inheritance（Template Method: `execute(self, state) -> dict` の override のみ）
  - Graph: composition（`register_nodes()` によるノード差し込み。外側グラフの `add_edges()` は override しない）

## Architecture Overview

### 全体構成（外側 backbone ＋ 内側 domain workflow）

```
外側（graph.py — AgentBaseGraph・固定5ノード backbone、add_edges() override なし）
START → initialize → pre_process → main(TripPlanWorkflowGraphNode)
      → {route} → post_process → finalize → END

内側（domain_workflow_graph.py — BaseGraph・mode 分岐トポロジ）
START → dispatch ─┬─ [mode=ingest]   → ingest_normalize → ingest_store → END
                  ├─ [mode=plan]     → policy_check → web_search → candidate_rank
                  │                     → plan_synthesize → hitl_refine ─┬→ END（幹事が確定）
                  │                        ↑            ↑               │
                  │                        │            └──(重み/条件調整)┘
                  │                        └──(幹事の明示要求時のみ 新規Web検索へ)
                  └─ [mode=feedback] → feedback_update → END
```

- 単一の `AgentBaseGraph` を `mode`（`ingest` / `plan` / `feedback`）で分岐する**単一グラフ・モード分岐**。`src/api/server.py` が 3 HTTP ルート（`/ingest` `/invoke` `/feedback`）を受け、いずれも同一グラフを `mode` 付きで invoke する（AgentCore single-entry 設計と整合）。
- 内側グラフは `src/graph/domain_workflow_graph.py` に置く（この path のみ許容。`src/subagents/` 不可）。
- AGENTIC STAR Marketplace はルート `cli.py` の `MarketplaceGraph` から入り、入力を read-only の `mode="plan"` context に再構成し、Azure LLM と SQLite repository を graph 構築前に注入する。

### Node Configuration — 外側（backbone 3 スロット）

| Node | Class / 継承 | Responsibility | Input State | Output State | required_trust_level |
|------|--------------|----------------|-------------|--------------|----------------------|
| initialize | InitializeNode（framework 注入） | — | — | — | —（framework 既定） |
| pre_process | `PreProcessNode` / FunctionNode | `mode` 判定と入力検証・正規化。plan モードは最小4項目（人数/予算感/時期/出発地）の充足確認と欠落項目の既定値補完（§Usability）。参加者配慮の生テキストを**匿名化集計**（氏名等の直接識別子を除去しカテゴリ×人数へ変換）。feedback モードの自由文も同フックで直接識別子を除去してから `validated_input` に載せる（生テキストを checkpoint に残さない）。S-2 extra フックで配慮情報・PII の入力スキャン | `user_input`, `input_context` | `validated_input`（mode+正規化済みパラメータの JSON 文字列）, `mode`, `assumption_notes`, `status` | `VERIFIED_EXTERNAL` |
| main | `TripPlanWorkflowGraphNode` / GraphNode | 内側 domain workflow の呼び出し（下表）。`extract_input()` で `validated_input` を渡し、`merge_output()` で内側 `get_output()` の結果を外側 State へ写像（変更キーのみ返却）。上流 `status=error` はパススルー | `validated_input`, `mode` | `result`（企画パッケージ本体 or ingest/feedback 受理結果）, `plan_id`, `quality_notes`, `status`, HITL 系（interrupt 時） | `VERIFIED_EXTERNAL` |
| post_process | `PostProcessNode` / FunctionNode | 出力整形: 稟議・社内回覧に貼れる Markdown 企画書＋機械可読 JSON の 2 形式を `output` に組み立て。S-3 extra フックで直接識別子（氏名・連絡先）の残存スキャンと credential 残存チェック | `result`, `plan_id`, `quality_notes` | `output`, `status` | `VERIFIED_EXTERNAL` |
| finalize | FinalizeNode（framework 注入） | — | — | — | —（framework 既定） |

### Node Configuration — 内側（DomainWorkflowGraph / BaseGraph 直継承・カスタムトポロジ）

全ノード FunctionNode 継承・`required_trust_level = VERIFIED_EXTERNAL` をクラス本体で明示宣言。

| Node | Responsibility | Input（内側 State） | Output（変更キーのみ） |
|------|----------------|--------------------|------------------------|
| `dispatch` | **内側グラフのエントリ。`user_input`（外側 `extract_input()` が渡す JSON 文字列）を `json.loads` でパースし、`request_params` / `mode` / `care_requirements` を内側 State へ復元（hydrate）する**。パース失敗・不正 mode は `status=error` | `user_input` | `request_params`, `mode`, `care_requirements`, `status` |
| `ingest_normalize` | 顧客アップロード（旅行規程 PDF テキスト・過去精算書/満足度アンケート CSV/Excel）を LLM 列マッピングで正規化（実証済みパターン）。参加者個人情報は**この段階で破棄**（非個人フィールドのみ通す） | `request_params`（ingest ペイロード） | `normalized_records`, `ingest_summary` |
| `ingest_store` | 規程 → `travel_policy` DB へ版管理付き UPSERT。過去実績 → `trip_records` DB ＋ 過去行程 KB（vector）へ INSERT。書き込み件数・スキップ理由を返す | `normalized_records` | `ingest_result`, `status` |
| `policy_check` | `travel_policy` DB を照合し制約セット（一人当上限・宿泊可否・承認フロー・禁止事項）と**根拠条項の引用**を作る。規程未整備なら縮退（`policy_status="unverified"`・出力に「要確認」注記） | `request_params` | `policy_constraints`, `policy_citations`, `policy_status` |
| `web_search` | Web 検索ツールで目的地×季節×予算の宿/交通/観光/費用相場を**実行時に動的取得**（公開情報は moat に数えない・会場 DB を事前構築しない）。retry budget（`web_search.max_retries`）超過で `web_search_ok=false` にして**過去行程 KB 実績で代替候補**を組む（fallback・品質注記付与） | `request_params`, `policy_constraints` | `web_results`, `web_search_ok`, `quality_notes` |
| `candidate_rank` | 過去行程 KB を類似条件（目的地エリア/時期/規模/予算帯）で検索し、`satisfaction_score` で加点・`complaint_flags` で回避、規程上限で除外、参加者配慮の充足（食事対応・バリアフリー等）をフラグ付け | `web_results`, `policy_constraints`, `care_requirements` | `ranked_candidates`, `kb_matches` |
| `plan_synthesize` | 規程準拠の **3 パターン旅程プラン**（日程/移動/宿/費用概算/根拠）を LLM で合成。HITL feedback があれば重み・条件を反映して再合成 | `ranked_candidates`, `policy_*`, `hitl_feedback` | `plans`, `selected_plan`（収束後） |
| `hitl_refine` | D6 `interrupt()` で 3 案＋規程チェック＋前提を幹事へ提示。**resume 応答解釈処理の先頭で `shared.security.pii_detector` により応答テキスト中の直接識別子（氏名・連絡先）を検出・除去し、除去後テキストのみを下流に使う**（§HITL 設計）。応答を解釈し分岐指示（§HITL）を返す。`hitl_draft` に提示済みプランを退避（resume 時の再生成防止） | `plans` | `hitl_draft`, `hitl_feedback`（除去後テキスト）, `refine_directive`, `status` |
| `feedback_update` | `plan_id` ＋実費/満足度/クレームを受領し `trip_records` を UPDATE、過去行程 KB へ実績追記。**クレーム自由文は S-2 extra フック＋LLM 正規化で `complaint_flags`（種別コード）と非個人サマリへ変換してから書き込む（従業員名等の直接識別子は DB/KB/State のいずれにも残さない）**。`organizer_edited`（幹事修正有無）も記録（改善シグナル）。書込失敗はベストエフォート（`status=success` 維持＋注記） | `request_params` | `feedback_result`, `status` |

内側 `route()`・`get_output()`・分岐関数は**自 schema（`State`）で注釈**する（`AgentState` 注釈だと LangGraph が AgentState に射影しカスタム欄が消えて誤 route する — Business Travel Hotel & Plan Recommendation Agent 実証）。

### GraphNode 契約（`TripPlanWorkflowGraphNode`）

- `get_subgraph()`: **`__init__` で生成した `self._subgraph` を返す（キャッシュ必須）**。毎回新規生成すると inner checkpointer が invoke/resume で別インスタンスになり、resume が suspend チェックポイントを見失って再実行 → 再 interrupt の無限ループになる（TourPlanningProcurementRoutingAgent 実機で確認済みの必須事項）。
- `extract_input()`: `state["validated_input"]`（pre_process が作る JSON 文字列。`json.dumps(..., ensure_ascii=False, default=str)`）を返す。内側グラフは `user_input`(str)+ctx のみ受領するため、mode・正規化済みパラメータ・配慮集計はすべてこの JSON に載せ、**内側の `dispatch` ノードが `json.loads` で内側 State（`request_params` / `mode` / `care_requirements`）へ復元する**（直列化と復元は対になる契約。フィールド名は §State Definition と一致させる）。
- `merge_output()`: 内側 `get_output()` の `{output, status, plan_id, quality_notes}` を外側 State の変更キーへ写像。**HITL 伝播時は `hitl_draft` / `hitl_feedback` / `hitl_status` も merge に含める**（欠落すると outer 側で HITL 状態が失われる — TourPlanningProcurementRoutingAgent 実証）。full state は返さない。
- `error_strategy = "propagate"`（既定・fail fast）。Web 検索失敗・DB 書込失敗は**内側ノードで fallback / ベストエフォート処理して success で返す**設計のため、GraphNode まで到達する例外は設計外の障害＝SubgraphError で即時失敗させ握り潰さない。
- `propagate_hitl = True`: 内側 `hitl_refine` の interrupt を外側 caller（幹事のクライアント）まで表面化する。外側 config でも `hitl.enabled: true` が必要。

### HITL 設計（壁打ち対話＝正常系）

- **パターン**: D6 `interrupt()` のみ（`hitl_refine.execute()` 内から呼ぶ）。`hitl_draft` に提示済み 3 案を退避し resume 時の LLM 再実行を防ぐ。
- **対話は価値の中核**: 幹事が「予算下げて」「日程ずらして」「温泉に変えて」と条件変更 → 再提案を繰り返す refine ループは**正常系**であり、回数上限で対話を縛らない。`max_hitl: 24` は暴走対策の**安全弁としてのみ**設定 — 想定壁打ち上限（〜10 往復）の約 2.4 倍の安全マージンで、テナントのコスト許容度に応じ 12〜48 の範囲で調整可。
- **Cat 2 境界**: refine は既取得候補への重み/フィルタ調整＋`plan_synthesize` 再実行が既定。**新規 Web 検索（`web_search` へ戻る）は幹事が明示的に「別の目的地/別の軸で探して」と要求した時のみ**（`refine_directive="new_search"`）。人間入力なしの自律再検索ループはしない＝Cat 3 化しない。
- **checkpointer**: `memory_enabled: true` 必須。**inner・outer の両方に明示 `compile(checkpointer=...)`**（1.0.0 wheel は config から checkpointer を自動生成しない。`server.py` は引数なし `agent.compile()` を使わない）。
- **禁止事項**: `hitl_count` / `hitl_allowed` はテンプレートから書き込まない（framework/backbone 専用）。max_hitl ブレーカーの自前実装もしない（Cat 2 では framework は max_hitl を検証のみ）。**書き込まないが読む — 下記ガードは必須。**
- **`hitl_allowed=false`（自動実行文脈）のガード＝必須**（framework の HITL 契約）: `hitl_refine.execute()` は `interrupt()` の前に `state.get("hitl_allowed", True)` を必ず確認する。本テンプレは `TripPlanWorkflowGraphNode(propagate_hitl=True)` の内側にあるため、1.0.0 wheel の `GraphNode.execute()` が `propagate_hitl and state.get("hitl_allowed", True)` を内側 `InvocationContext` に流し込む — 親エージェント合成や Gateway の `X-HITL-Allowed: false` が**この内側ノードまで届く**。無条件 `interrupt()` は誰も resume できない文脈でグラフを suspend させる＝本番デッドロック（blocking defect）。
- **既知の制約（framework 側・`request:coe` 対象）— resume 経路の `hitl_allowed` は反映できない**: 上記ガードが効くのは **初回 invoke 経路**まで。1.0.0 wheel の `GraphNode.execute()` は `subgraph_thread_id` がある resume 経路で `hitl_allowed` を参照せず無条件に `lg_interrupt()` を呼び（`framework/nodes/graph_node.py`）、`BaseGraph.resume(thread_id, feedback)` は `InvocationContext` を受け取らない。したがって「HITL 可で suspend → その後に親/Gateway が HITL 不可へ変更 → resume」した場合、本テンプレのガードに到達する前に外側グラフが再 suspend する。**テンプレート層では塞げない**（`lg_interrupt()` を条件化するだけでは内側 checkpoint が未解決のまま残るため不十分）。恒久対応は framework 側で resume 時に信頼済み context/state 更新を適用する必要がある。なお HTTP adapter（`src/api/server.py`）側は初回 invoke について、認証 middleware が設定した `request.state.hitl_allowed` を `InvocationContext` に反映する（クライアントの生ヘッダは信頼しない）。
- **非 HITL 時の決定論的出力（選定は代行しない）**: ガード発火時は `refine_directive="approve"` で END へ抜け（ルーティング契約は不変）、**`selected_plan` は空のまま**にして合成済み全プランを返す。案の選定は幹事の判断であり、`plans[0]` を黙って確定させると「誰も選んでいない案」が承認済みとして下流に流れる。`quality_notes` に「HITL 未実施」を明記し、S-4 イベント `hitl_skipped_not_allowed`（理由・案数のみ）を emit する。`post_process` も同期しており、`selected_plan` 空時は単一の「行程表」ではなく**「候補プラン（幹事による選定が未了）」として全案を列挙**し、JSON 側は `selection_status: "pending_organizer"` / `plan: {}` / `candidate_plans: [...]` を返す（機械的消費者が「未選定」を判別できるようにするため）。
- **resume 経路**: `/invoke` の同一 `session_id` で resume。server.py は「同一 `thread_id` に suspend 中の checkpoint がある」場合を resume と判定し、`Command(resume=<幹事応答テキスト>)` でグラフを再開する。**LangGraph の resume は suspend した checkpoint（`hitl_refine` の `interrupt()` 地点）から再開するため、initialize / pre_process は再実行されない** — したがって resume リクエストが pre_process の最小 4 項目チェックを通る必要はない（初回 invoke のみ pre_process を通過する）。`hitl_refine` が応答を `refine_directive` に解釈し、`approve` → END（`selected_plan` 確定）、`adjust` → `plan_synthesize`、`new_search`（明示要求）→ `web_search`。
- **resume 応答の軽量識別子除去（S-2 バイパス対策）**: resume 経路は pre_process の S-2 拡張ゲートを通らないため、幹事応答に個人名が混ざるとそのまま checkpoint へ永続化される穴がある。対策として **`hitl_refine` の resume 応答解釈処理の先頭で `shared.security.pii_detector` による直接識別子（氏名・連絡先）の検出・除去を実施**し、**除去後のテキストのみ**を (a) `refine_directive` の解釈、(b) `hitl_feedback` 由来の State 反映、(c) `plan_synthesize` への伝達に使う。生の応答テキストはノード戻り値に載せない。除去が発生した場合は S-4 イベント `hitl_response_sanitized`（**除去件数のみ**・除去内容は記録しない）を emit する。

### State Definition

外側は `src/schemas/state.py` の `State`（`AgentState` 継承 flat TypedDict）。内側も同一 `State` を使う（GraphNode の入出力写像を単純化）。**全フィールド msgpack 可能な primitives / list / dict のみ。**

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| `mode` | `str` | `ingest` / `plan` / `feedback` | ✅ |
| `validated_input` | `str` | pre_process が正規化した JSON 文字列（内側への引き渡し） | ✅ |
| `request_params` | `dict` | 正規化済み入力（人数/予算帯/時期/出発地/目的地候補/配慮集計） | ✅ |
| `assumption_notes` | `list[str]` | 欠落入力に対する既定値・前提の明示（出力に転記） | — |
| `policy_constraints` | `list[dict]` | 規程制約セット（上限額・宿泊可否・禁止事項） | —（plan 時） |
| `policy_citations` | `list[dict]` | 照合根拠の条項引用（規程名・版・条番号・抜粋） | — |
| `policy_status` | `str` | `verified` / `unverified`（規程未整備の縮退） | — |
| `care_requirements` | `list[dict]` | **匿名化集計済み**の配慮事項（カテゴリ×人数。氏名・個人紐付けなし） | — |
| `web_results` | `list[dict]` | Web 検索で取得した候補（宿/交通/相場・出典 URL） | — |
| `web_search_ok` | `bool` | fallback 発動判定 | — |
| `kb_matches` | `list[dict]` | 過去行程 KB の類似実績（満足度・クレームフラグ含む） | — |
| `ranked_candidates` | `list[dict]` | 規程除外・実績加点済みの候補リスト | — |
| `plans` | `list[dict]` | 3 パターンの旅程プラン（日程/移動/宿/費用概算/根拠） | — |
| `selected_plan` | `dict` | HITL 収束後の確定寄りプラン | — |
| `refine_directive` | `str` | HITL 応答の解釈結果（`approve` / `adjust` / `new_search`） | — |
| `plan_id` | `str` | 企画レコード ID（`/feedback` の参照キー） | — |
| `quality_notes` | `list[str]` | fallback・縮退の品質注記（出力に転記） | — |
| `normalized_records` | `list[dict]` | ingest の正規化済みレコード（非個人フィールドのみ） | —（ingest 時） |
| `ingest_summary` | `dict` | ingest_normalize の正規化サマリ（列マッピング結果・破棄列） | — |
| `ingest_result` / `feedback_result` | `dict` | 取込/更新の受理結果 | — |
| `hitl_draft` | `str\|list\|dict\|None` | 提示済み 3 案の退避（resume 時の再生成防止。framework schema 1.4 のテンプレート書込可フィールド） | —（HITL 時） |
| `hitl_feedback` | `str\|dict\|None` | 幹事応答（resume 後に framework が書込） | — |
| `hitl_status` / `hitl_metadata` | — | framework 書込専用（テンプレートは読み取りのみ） | — |
| `hitl_count` / `hitl_allowed` | — | **framework/Gateway 専用 — テンプレートは読み書きしない** | — |
| `result` / `output` | `dict` / `dict` | main の成果物 / post_process の最終出力（Markdown＋JSON） | ✅ |

**State Constraints (mandatory):**
- Flat TypedDict のみ（primitives + JSON-serializable types）
- JWT・API key・credential を State に置かない（checkpoint DB へ平文永続化されるため）
- InvocationContext は `config["configurable"]` 経由のみ（State に入れない）
- Pydantic / dataclass / 任意 Python オブジェクト禁止（msgpack 非互換）
- **参加者配慮の個人紐付け実値（氏名×アレルギー等）はいかなるノード戻り値にも載せない**（checkpoint 残留防止・C-01）。State に入るのは匿名化集計（カテゴリ×人数）のみ。DB/KB へは非個人の旅行実績フィールドのみ INSERT

> **route 注釈の適用範囲の明確化**: 外側の `route()` は framework 実装（`state["status"]` のみ参照・override しない）ためカスタム欄の射影問題は生じない。自 schema 注釈が必須なのは**内側グラフの分岐関数・`get_output()`** であり、既に §内側ノード表のとおり `State` 注釈を規定済み。

## Security Design

### S-1 Trust Gate
- 全 FunctionNode（外側 pre_process/post_process・内側全 8 ノード）で `required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL` をクラス本体で明示宣言（暗黙 ANONYMOUS 継承は不可）。有効値は `ANONYMOUS` / `VERIFIED_EXTERNAL` / `INTERNAL` のみ。
- 根拠: 組織固有の規程 DB・過去実績 KB の読み書きと社内向け企画生成＝標準業務操作。呼び出し元は JWT/OAuth 検証済みの社内ユーザー（幹事）。`INTERNAL` を要求すると幹事の通常利用を遮断するため過剰。

### S-2 Input Security Gate（`_extra_security_gate_input()` フック）
- FunctionNode の `@final _security_gate_input()`（標準 PII スキャン）は触らず、`_extra_security_gate_input()` で拡張:
  - `PreProcessNode`: 参加者配慮の生テキストに含まれる氏名・連絡先等の直接識別子を検出（`shared.security.pii_detector` 併用）し、**匿名化集計（カテゴリ×人数）へ変換してから下流へ渡す**。変換不能な個人紐付け入力は `SecurityViolationError` ではなく警告付き除去（幹事の利便性を壊さない）。
  - `ingest_normalize`: アップロードデータの個人情報列（氏名・社員番号・連絡先）を検出し**取込前に破棄**。
  - `feedback_update`: クレーム・満足度の自由文に含まれる従業員名等の直接識別子を検出・除去し、`complaint_flags`（種別コード）＋非個人サマリのみを DB/KB へ書き込む（/feedback 経路にも S-2 拡張を必ず適用）。
  - `hitl_refine`（**resume 経路の補完ゲート**）: resume 応答は pre_process の S-2 拡張を通らないため、応答解釈処理の先頭で `shared.security.pii_detector` により直接識別子を検出・除去する（§HITL 設計「resume 応答の軽量識別子除去」）。除去後テキストのみを State・下流ノードへ渡す。
- `TripPlanWorkflowGraphNode`（GraphNode）は ADR-017 のとおり deliberate no-op passthrough（ゲートは内側各 FunctionNode で実施済み）。

### S-3 Output Security Gate（`_extra_security_gate_output()` フック）

**防御の主従（重要）**: C-01（参加者の配慮情報は匿名化集計のみ）の主防御は **出力キーの closed allowlist**（`_project_plan()` の `_PLAN_PUBLIC_FIELDS`）であり、識別子スキャンは**補助線**である。allowlist は「何が外に出られるか」を構造的に決めるので機械で閉じるが、識別子スキャンは自由文の中身を当てにいくため原理的に閉じない。**スキャンの網羅性を主防御に据えてはならない**（Return Fraud Investigation Memo Drafting Agent の lexicon 硬化 whack-a-mole 事例）。最終担保は HITL の人間レビュー。

- `PostProcessNode._extra_security_gate_output()`: 最終出力（Markdown/JSON）に対し (a) 直接識別子（氏名・連絡先）の残存スキャン、(b) credential パターン再チェック、(c) 対応必要事項リストが「カテゴリ×人数」形式に収まっていることの検証。
- **走査範囲**: framework の `@final` S-3 は結果のトップレベル文字列しか見ないため、ネストした JSON はテンプレ側で走査する（`_collect_output_strings()`）。走査できない型（`set` / `bytes` / 非文字列キー / 循環参照 / 深さ超過 / 未知オブジェクト）は**スキップせず出力を拒否する（fail closed）** — 見落として通すより安全側に倒す。
- **検知限界（受容済み・網羅保証ではない）**: 検出器は `shared.security.pii_detector` ＋ `identifier_sanitizer` の補完パターンで、**敬称なしの日本語氏名（例「佐藤花子」）は検知できない**。敬称なし漢字列を氏名とみなすと「箱根温泉」「山田旅館」など旅程の中核語彙を誤検知で潰すため、**過検知とのトレードオフとして非検知を受容する**（2026-07-06 MR-B で確定。`docs/0A_release_note.md` §制約・免責に利用者向けの既知事項として記載済み — 「敬称の付かない裸の氏名」「役職を伴う表記」「外国人名・カタカナ表記」は残る、一次防御は氏名を入力しない運用、と明示）。`phone_us` の「直後に日本語が続く」形も同様に未補完。これらは検出器（vendored `shared/`）側の課題であり、テンプレ側でパターンを積み増して閉じにいかない — 恒久対応は `request:coe`。
- 内側ノードは標準 credential スキャン＋必要最小限の extra フックのみ。

### S-4 Audit Logging
- 全ノードの `execute()` 内で `emit_trace_event()`（`shared.utils.audit_logger`・3 引数）を**最低 1 回**呼ぶ（副作用の有無に関わらず）。ドメインイベント例: `policy_checked`（照合条項数）、`web_search_completed` / `web_search_fallback`、`candidates_ranked`、`plans_synthesized`、`hitl_interrupt_raised` / `hitl_resumed`、`hitl_response_sanitized`（resume 応答からの識別子除去発生時・**除去件数のみ**）、`ingest_stored`（件数）、`feedback_recorded`。
- `node_start` / `node_complete` / `node_error` は `__call__()` が自動発行するため execute() 内で重複発行しない。

### S-5 / Secrets
- 秘匿値を実配線する場合は API アダプタ層の secrets provider から解決する（`os.environ` 禁止）。State・ソースコードに credential を置かない。テストは明示モック値（例: `"mock-jwt-for-testing"`）。
- SQLite ファイルパスは `src/api/server.py` が `SecretProvider.require("TRIP_DB_URL")` で解決する。`config/agent.yaml` の `requires.secrets` に同キーを宣言する。
- 過去行程 KB を有効化した環境では、Qdrant URL、Azure embedding API key、endpoint、deployment 名を `SecretProvider` から解決する。空・空白のみの値は SDK client 構築前に拒否し、State や `config/config.yaml` には格納しない。
- `pyproject.toml` は全依存 `==` 完全 pin（`gate-dep-pinning`）。

## Framework Utilization

### Shared Components Used
- [x] InvocationContext（correlation_id, session_id, caller_trust_level, hitl_allowed — `config["configurable"]` 経由）
- [x] ConnectionPolicy（Web 検索・DB の retry/timeout 戦略）
- [x] SecurityViolationError
- [x] S-2: `_extra_security_gate_input()` — 配慮情報の匿名化検証・ingest の個人情報列破棄（上記）
- [x] S-3: `_extra_security_gate_output()` — 直接識別子残存スキャン・credential 再チェック（上記）
- [x] S-4: `emit_trace_event()` — 各 `execute()` 内で最低 1 回（上記イベント一覧）
- [x] `shared.services.llm.BaseLLM` — LLM クライアントはコンストラクタ注入（テストは fake 注入）
- [x] `shared.security.pii_detector` — S-2 拡張で併用
- [x] `shared.services.database` — `travel_policy` / `trip_records`（テナント内 DB。実証済みパターン）

### Composition Pattern
- **Pattern**: GraphNode (subgraph)
- **Composition target**: `src/graph/domain_workflow_graph.py`（`BaseGraph` 直継承・mode 分岐カスタムトポロジ）
- **Error propagation strategy**: **propagate**（既定）。業務上想定される失敗（Web 検索不調・DB 書込不調）は内側ノードが fallback / ベストエフォートで吸収して `status=success`＋`quality_notes` で返すため、GraphNode 境界に届く例外は設計外障害＝SubgraphError で fail fast。

## Services 層（`src/services/`）

| Module | Responsibility |
|---|---|
| `sqlite_database.py` | SDK `BaseDatabase` を実装する暫定 SQLite backend。下記 2 テーブルの DDL と JSON / boolean 型変換を担当 |
| `trip_repository_factory.py` | `policy_database` / `trip_records_database` の backend 選択を一元化する差し替え点 |
| `policy_service.py` | `travel_policy` DB の版管理付き UPSERT / 制約照合（条項引用付き） |
| `trip_records_service.py` | `trip_records` DB の INSERT / UPDATE（`plan_id` 採番・`satisfaction_score` 後追い更新） |
| `trip_kb_service.py` | 過去行程 KB（vector）の upsert / 類似実績検索（1 実施旅行＝1 チャンク） |
| `trip_kb_qdrant_adapter.py` | 業務 `plan_id` を `metadata.source_id` に保持し、SDK point ID を canonical UUID、本文を `content` へ変換する Qdrant 境界 |
| `web_search_service.py` | Web 検索ツール呼び出し（retry budget・timeout・結果の妥当性チェック） |
| `llm_client.py` | `BaseLLM` 注入シーム（列マッピング正規化・プラン合成・HITL 応答解釈） |

DB スキーマ（`trip_records`: `plan_id` PK / `destination_area` / `season` / `party_size` / `budget_band` / `lodging` / `actual_cost` / `satisfaction_score` / `complaint_flags` / `organizer_edited` / `created_at`）には、**個人名・連絡先・配慮実値のカラムを持たせない**（C-01）。

## config/agent.yaml マニフェスト

```yaml
id: "CMN-C2-655"
name: "CorporateGroupTripPlannerAgent"
namespace: "cmn"
version: "0.1.0"
enabled: true
category: "Cat 2"
generation_mode: "llm"
industry: "CMN"
base_type: "AgentBaseGraph"
class: "src.graph.graph.CorporateGroupTripPlannerGraph"
required_trust_level: "VERIFIED_EXTERNAL"
requires:
  secrets:
    - TRIP_DB_URL
    - TRIP_KB_QDRANT_URL
    - TRIP_KB_EMBEDDING_API_KEY
    - TRIP_KB_EMBEDDING_ENDPOINT
    - TRIP_KB_EMBEDDING_DEPLOYMENT
  extras: []
```

## config/config.yaml 主要キー設計値

```yaml
max_retry: 3
timeout_seconds: 60
memory_enabled: true              # HITL 必須要件（checkpointer）
hitl:
  enabled: true
  max_hitl: 24                    # 暴走対策の安全弁のみ。壁打ち対話を縛る意図の上限ではない
plans_count: 3
llm:
  model: "claude-sonnet"
  temperature: 0.2
  max_tokens: 4000
web_search:
  max_retries: 2
  timeout_seconds: 10
ingest:
  max_rows_per_call: 500
vector_store:
  enabled: false                 # 無効時は DB 保存のみ継続
  collection_name: "cmn_c2_655_trip_kb"
  namespace: "cmn"
  agent_name: "cmn_c2_655"
  vector_size: 1536
embedding:
  dimensions: 1536              # vector_size と不一致なら構築時エラー
  provider: "azure"
```

過去行程検索に AG 独自の数値閾値は設けない。SDK `QdrantVectorStore` の固定
`score_threshold=0.4` を使用し、0.4 未満の結果を必要とする設計要求はない。
検索後に利用するのは旅行条件・満足度・クレームフラグ等の metadata であり、
本文全文は下流ノードで参照しない。そのため本文を予約 payload キーへ複製する
round-trip は行わず、投入時の embedding 対象としてのみ `content` へ渡す。

## API / I-O 設計（`src/api/server.py`）

| Route | mode | 入力（最小） | 出力 |
|---|---|---|---|
| `POST /ingest` | ingest | 規程 PDF テキスト or 精算書/アンケート CSV/Excel（生データ＋任意の列ヒント）。**受け入れ契約: ペイロードはテキスト化済みデータのみ受領（規程 = text/plain 抽出済みテキスト・実績 = CSV/Excel 由来の行配列 JSON）。1 リクエスト上限: 500 行 / 2MB（超過は分割投入を案内するエラー）** | 取込件数・スキップ理由・規程版番号 |
| `POST /invoke` | plan | **最小 4 項目: 人数・予算感・時期・出発地**（自由文でも可。目的地候補・配慮集計は任意） | 3 旅程プラン（→HITL で 1 案収束）＋規程チェック＋対応必要事項＋幹事タスク（Markdown＋JSON）＋`plan_id` |
| `POST /invoke`（resume） | plan | `session_id`＋幹事の応答テキスト（`Command(resume=...)` で suspend 地点から再開。**pre_process は再実行されない**ため 4 項目チェック対象外） | refine 後のプラン or 確定パッケージ |
| `POST /feedback` | feedback | `plan_id`＋実費/満足度/クレーム（自由文可） | 更新受理結果 |

- AgentRegistry は `config/config.yaml` を `Graph(config=...)` に渡す。standalone の server.py も同値の設定で構築し、**明示 `compile(checkpointer=...)`** で HITL resume を有効化する（引数なし `compile()` 禁止）。
- センシティブ値の resolve が必要になった場合は API アダプタ層でのみ行う（グラフ内ノード戻り値には載せない）。本テンプレートは匿名化集計方式のため通常 resolve 不要。

## Usability 設計判断（ユーザビリティレビューの結果・修正点）

設計時にエンドユーザー（幹事）のユースケースでセルフレビューし、以下を**当初案から修正・確定**した:

1. **初回入力の最小化**: 当初案は目的地候補・配慮事項を含む全項目入力を想定していたが、**「人数・予算感・時期・出発地」の 4 項目（自由文可）だけで開始できる**よう pre_process を修正。欠落項目は既定値（例: 1 泊 2 日・出発地起点 3 時間圏）で補完し、`assumption_notes` として出力冒頭に「前提」を明示 → 幹事は HITL で前提ごと修正できる。全項目必須のフォームは幹事の着手障壁になるため排除。
2. **稟議にそのまま貼れる出力**: 最終出力を「社内回覧・稟議添付を想定した Markdown 企画書（行程表・費用概算表・規程チェック結果〔条項引用付き〕・参加者対応必要事項・期限付き幹事タスク）」＋機械可読 JSON の 2 形式に確定。規程チェックに条項引用を必ず添えることで、承認者（総務 SV/管理職）がそのまま判断できる。**幹事タスク欄には固定文言 1 行「配慮対象者の個別対応は名簿と突き合わせて実施（AG は個人紐付けを保持しない）」を必ず含める**（匿名化集計方式の運用前提を出力側で明示・ユーザー判断 2026-07-06 反映）。
3. **参加者配慮の収集を幹事の手入力に依存させない**: 幹事が全員分の配慮を手で構造化入力するのは非現実的。(a) `/ingest` で既存の参加者アンケート CSV を取り込み（個人情報列は取込前破棄・匿名化集計へ変換）、(b) plan 出力に**参加者向け配慮アンケートのテンプレート文面**（配布用）を同梱、(c) `/invoke` では自由文サマリ（「ベジタリアン 2 名・車椅子 1 名」）をそのまま受ける — の 3 経路を用意。
4. **壁打ち対話を正常系として設計**: HITL refine を「低信頼時の例外フロー」ではなく主要ユースケースとして `hitl_refine` ノードに独立させ、`max_hitl: 24` を安全弁に留めた（対話回数で価値を縛らない）。新規 Web 検索のみ明示要求制で Cat 2 境界を維持。
5. **失敗時にユーザーへ返すもの**: Web 検索失敗 → 過去行程 KB 実績で代替プランを組み `quality_notes`（「相場情報は 2024 年実績ベース」等）を出力に明示して success で返す。規程 DB 未整備 → 規程チェックを「要確認」注記付きで縮退。過去 KB 空 → 実績ランクのみ縮退し Web 検索ベースで提示。**いずれも黙って品質を落とさず、注記で幹事に伝える。**

## Import Isolation Confirmation
- [x] Template does not import agenticstar-platform SDK (Level 0)
- [x] Import targets: `framework/` and `shared/` only（agents/base/ 不使用・他テンプレート import なし）

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | **A** | 固定パイプライン＋人間駆動 HITL。自律 think→act ループではない（Cat 2） |
| Composition pattern | Standalone（main=FunctionNode） | GraphNode subgraph | **B** | mode 分岐×多段（照合→検索→ランク→合成→HITL）は内側カスタムトポロジが必要 |
| 内側グラフ基底 | BaseGraph（カスタム） | AgentBaseGraph（5 ノード） | **A** | ingest/plan/feedback の 3 分岐と HITL ループは backbone に載らない |
| 3 モードの実現 | 単一グラフ・mode 分岐 | モード別 3 グラフ | **A** | AgentCore single-entry 設計と整合。データ資産ライフサイクル（取込→企画→蓄積）は単一責務 |
| HITL 上限 | max_hitl 小（2〜3） | 安全弁として大きく（24） | **B** | 壁打ち対話が価値の中核。回数で縛るのはユースケース殺し。上限は暴走対策のみ |
| Web 検索失敗時 | error で返す | 過去 KB 実績で fallback＋品質注記 | **B** | 幹事は「今日ドラフトが欲しい」。縮退でも前に進める＋注記で透明化 |
| error_strategy | propagate | handle | **A** | 業務想定内の失敗は内側で吸収済み。境界到達例外は設計外＝fail fast が安全 |
| 配慮情報の扱い | 個人紐付けで保持 | 匿名化集計のみ State/出力に | **B** | C-01。checkpoint 永続化・DB 残留を根本回避。企画価値は集計粒度で成立 |
| base_type 表記 | RAGAgent（概念パターン名） | AgentBaseGraph（L1 クラス名） | **B** | L1 クラス名に統一（Business Travel Hotel & Plan Recommendation Agent・Enterprise Deal Scoring & Pipeline Priority Agent と同形式）。概念パターン（蓄積型 RAG）はコメント表記 |

## Test 方針（テストフェーズで docs/03_test_spec.md に詳細化）

- TC-01〜TC-08（framework 準拠）＋ PB-1〜PB-7（PB-7 は HITL 有効のため必須: `interrupt()` → `GraphInterrupt` がアプリ error boundary に捕まれず伝播すること）。
- ドメイン固有: 規程照合（上限超過プランの除外）・fallback（Web 検索全滅→KB 代替＋quality_notes）・HITL resume→success 到達（subgraph キャッシュ検証。suspend 止まりの PB だけでは無限ループを捕捉できない）・checkpoint 全量に配慮実値/credential が残らないこと・ingest の個人情報列破棄。
- **resume 応答サニタイズ PB（ユーザー判断 2026-07-06 反映）**: resume 応答に個人名（例: 「田中さんはベジタリアンなので和食に」）を含むケースで、resume→完了後の**全 checkpoint・全 State スナップショットに当該識別子が残らない**こと、`hitl_response_sanitized` イベントが除去件数付きで emit されること、除去後テキストで `refine_directive` が正しく解釈されることを assert する。
