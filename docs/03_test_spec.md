# Test Specification — CMN-C2-655 CorporateGroupTripPlannerAgent

## Test Strategy
- Coverage target: TC-01〜TC-08 + PB-1〜PB-7 全項目 + ドメイン固有項目（下記）100%
- Test types: Unit（`tests/unit/`）/ Integration（`tests/integration/`）/ Proof-of-Boundary（`tests/proof_of_boundary/`）
- 実行結果: 全 110 件 PASS（unit 81 + integration 16 + PB 13。内訳は下表。push 前レビュー指摘の
  回帰テスト `tests/unit/test_codex_review_fixes.py`・`tests/integration/test_service_wiring.py` を含む）
- 外部サービス（DB/vector store/LLM/Web検索）はすべて注入シームで fake 差替え（実接続は STG、`docs/07_operation_guide.md` 参照）

## Framework Compliance Tests (Mandatory)

| TC-ID | Test | Expected Result | Result |
|-------|------|----------------|--------|
| TC-01 | State contract: flat TypedDict | Type check pass, no Pydantic/dataclass | PASS（`tests/proof_of_boundary/test_state_safety.py`） |
| TC-02 | SecurityViolationError fires on invalid input | Error raised | PASS（`test_pre_process_node.py::test_unknown_mode_errors` 等・不正 mode/JSON は status=error で拒否） |
| TC-03 | No JWT/Credential in State | CI `gate-credential-scan`: 0 violations | PASS（`tests/proof_of_boundary/test_pb_checkpoint_safety.py::test_no_credential_pattern_in_any_checkpoint`） |
| TC-04 | InvocationContext via configurable only | Direct access raises error | PASS（全ノードで `config["configurable"]` 経由のみ・State に InvocationContext を格納しない設計） |
| TC-05 | S-4: no duplicate lifecycle events in `execute()` | `node_start`/`node_complete`/`node_error` absent from `execute()` body | PASS（`tests/proof_of_boundary/test_pb_invoke_order.py`） |
| TC-06 | S-2: `_security_gate_input()` not overridden（`FunctionNode` subclass） | `TypeError` at class definition if overridden | PASS（全 `FunctionNode` 派生は `_extra_security_gate_input()` のみ実装） |
| TC-07 | S-3: `_security_gate_output()` not overridden（`FunctionNode` subclass） | `TypeError` at class definition if overridden | PASS（全 `FunctionNode` 派生は `_extra_security_gate_output()` のみ実装） |
| TC-08 | `required_trust_level` enforced | Insufficient trust → refused | PASS（`gate-trust-level-check` CI + 全ノード `VERIFIED_EXTERNAL` 明示宣言） |
| TC-09 | S-2: `_extra_security_gate_input()` non-trivial | Domain checks execute correctly | PASS（`PreProcessNode`: 配慮情報匿名化＋PII検知イベント／`hitl_refine`・`feedback_update`: resume/complaint 識別子除去） |
| TC-10 | S-3: `_extra_security_gate_output()` non-trivial | Domain checks execute correctly | PASS（`PostProcessNode`: 直接識別子残存スキャン→検出時 `RuntimeError`・`tests/unit/test_post_process_node.py::TestPostProcessNodeS3Gate`） |
| TC-11 | S-4: ≥1 domain `emit_trace_event()` per node | Domain event emitted on every path | PASS（`tests/proof_of_boundary/test_pb_audit_logging.py`。GraphNode 境界の扱いは同ファイル参照） |

## Proof-of-Boundary Tests (Mandatory)

| PB-ID | Boundary | Test | Expected Result | Result |
|-------|----------|------|----------------|--------|
| PB-1 | BaseNode → AuditLogger | `emit_trace_event()` fires on every invocation path（副作用有無に関わらず） | No silent failures | PASS（`test_pb_audit_logging.py`） |
| PB-2 | State serialization | Post-invoke State is primitives only | No Pydantic/dataclass | PASS（`test_state_safety.py` + `test_pb_checkpoint_safety.py`） |
| PB-3 | L1 → External service | Template connects via `BaseDatabase`/`BaseVectorStore` 抽象、実データ取得 | Data retrieved | PASS（DB は `test_trip_repository_contract.py` で実 SQLite backend の公開サービス契約を検証。vector は SDK PgVectorStore が STUB かつ承認済み Qdrant 部品が本 repo にないため `test_pb_external_service.py` の fake による境界検証を維持） |
| PB-4 | Import isolation | Template does not import Level 0 | AST scan: 0 violations | PASS（`test_import_isolation.py`） |
| PB-5 | Checkpoint safety | Saved checkpoint に JWT/Pydantic/参加者識別子なし | Inspection pass | PASS（`test_pb_checkpoint_safety.py` — 内側・外側 checkpoint 両方を invoke→resume→success の全ライフサイクルで走査） |
| PB-6 | Invoke execution order | `__call__()`: S-1→S-4 `node_start`→S-2→`execute()`→S-3→S-4 `node_complete` | Order verified | PASS（`test_pb_invoke_order.py`。`HitlRefineNode`/`TripPlanWorkflowGraphNode` はグラフ実行コンテキスト必須のため同ファイル冒頭コメントの理由で対象外＝`tests/integration/test_hitl_flow.py` で実地検証） |
| PB-7 | HITL interrupt propagation（`hitl.enabled: true`） | (A) `interrupt()` → `GraphInterrupt` がアプリ error boundary に捕まれず伝播。(B) `hitl_allowed=false` ガードで interrupt が発火せず、**合成パス**でも suspend せず終端 | (A) `status` は `awaiting_human`（`error` にならない）。(B) `GraphInterrupt` が発生せず `status=success`・選定未了が出力に明示される | PASS（`test_pb7_hitl_interrupt_propagation.py`（exact-name・6 ケース） + `test_hitl_flow.py::TestHitlInterruptPropagation`） |

> **PB-7 のファイル名**: `tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py` は exact-name で必須。旧 `test_pb_hitl_interrupt.py` は本ファイルへ統合・削除した。条件判定は `config/config.yaml` の `hitl.enabled` を読み、ファイルがない・壊れている・`hitl` が mapping でない場合は黙ってスキップせず警告する。`pyyaml` は dev 依存として exact pin する。

### Persistence contract

`tests/contract/test_trip_repository_contract.py` の 1 ファイルで backend 共通の受入契約を固定する。対象は、factory へ明示注入した backend が SQLite パスより優先され policy / trip_records の両操作を受けること、注入 backend を factory が所有・close しないこと、規程の `org_unit` と版識別が完全一致であること、版別 upsert と最新施行版の参照、trip record の作成・feedback 更新、JSON 配列と boolean を含むドメイン型の round-trip、未登録 id / org_unit の安定した not-found 動作である。assertion は呼出事実・パラメータおよび `PolicyService` / `TripRecordsService` の公開 API に対して行い、SQL 文、`LIKE` の挙動、全文検索の語分割には依存しない。

## Domain-Specific Tests（docs/02_design.md §Test方針）

| ID | Test | Expected Result | Result |
|----|------|----------------|--------|
| D-01 | 規程照合で上限超過候補を除外 | `policy_constraints` の `per_person_cap` 超過候補は `ranked_candidates` に含まれない | PASS（`test_candidate_rank_node.py::test_excludes_over_policy_cap`） |
| D-02 | Web検索全滅→過去KB実績で fallback＋quality_notes | `web_search_ok=False`＋`quality_notes` に品質注記 | PASS（`test_web_search_node.py`・`test_plan_flow_degradation.py`） |
| D-03 | HITL resume→success 到達（subgraph キャッシュ検証） | suspend→resume（adjust）→resume（approve）で `status=success` に到達（無限 suspend ループなし） | PASS（`test_hitl_flow.py::TestHitlResumeToSuccess`） |
| D-04 | checkpoint 全量に配慮実値/識別子/credential が残らないこと | 全 checkpoint（内側・外側・invoke〜resume〜success 全断面）を dump してスキャン | PASS（`test_pb_checkpoint_safety.py`・`test_hitl_flow.py::TestHitlResponseSanitization`） |
| D-05 | ingest の個人情報列破棄 | 氏名・連絡先等の列は `normalized_records`/出力のいずれにも残らない | PASS（`test_ingest_normalize_node.py`・`test_ingest_flow.py::TestIngestPersonalColumnDiscard`） |
| D-06 | **resume 応答サニタイズ（ユーザー判断 2026-07-06 反映）** | resume 応答中の識別子（氏名・連絡先）が resume→完了後の全 checkpoint・全 State スナップショットに残らないこと。`hitl_response_sanitized` イベントが除去件数のみで emit されること。除去後テキストで `refine_directive` が正しく解釈されること | PASS（`test_hitl_flow.py::TestHitlResponseSanitization`・`test_identifier_sanitizer.py`・`test_hitl_refine_node.py`） |
| D-07 | 幹事タスクに固定文言が必ず含まれる | 出力 Markdown に「配慮対象者の個別対応は名簿と突き合わせて実施（AG は個人紐付けを保持しない）」が1行含まれる | PASS（`test_post_process_node.py::test_fixed_care_task_line_present`） |
| D-08 | 新規Web検索は明示要求時のみ（Cat 2 境界） | 「別の目的地を探して」等の明示文言でのみ `refine_directive=new_search` | PASS（`test_hitl_refine_node.py::test_new_search_keyword_explicit`・`test_hitl_flow.py::TestHitlNewSearchExplicitOnly`） |
| D-09 | 3モード（ingest/plan/feedback）が単一グラフの mode 分岐で正しく動作 | 各モードで正しい内側ノード列を通過し success を返す | PASS（`test_ingest_flow.py`・`test_hitl_flow.py`・`test_feedback_flow.py`） |

## MR-B 実装時に発見した SDK 既知不備（要ユーザー判断・最終レポート参照）

実装・テスト中に `shared.security.pii_detector.detect_pii()`（vendored, 編集禁止）に以下の検知漏れを発見し、
`src/services/identifier_sanitizer.py` の補完正規表現レイヤーで対策済み（詳細は同モジュール docstring）:

1. 日本語氏名の検知が一切ない（`name` パターンは Latin-script Title-Case ヒューリスティックのみ）
2. `email`/`phone_jp` 等、末尾 `\b` を伴うパターンは、識別子の直後に区切りなしで日本語テキストが続くと **マッチ全体が失敗**する（Python の Unicode 対応 `\b` が ASCII 文字と日本語文字を共に「word文字」とみなすため境界が存在しない）

`tests/unit/test_identifier_sanitizer.py::TestDetectPiiKnownGaps` にこの不備を固定するリグレッションテストを追加済み（SDK 側で修正された場合はテスト側で検知される）。

## Push 前レビュー指摘（実装済み修正）

実装フェーズの push 前レビューで実ファイルベースの指摘 6 件を受け、全て修正済み（回帰テスト付き）:

| # | 指摘 | 深刻度 | 修正 | 回帰テスト |
|---|------|--------|------|-----------|
| 1 | `wire_services()` が `agent.compile()` の後に実行されていたため、実DB/LLM/Web検索クライアントが内側グラフに永久に届かない | High | `src/api/server.py`: `build_agent_config()` で config を先に完成させてから `CorporateGroupTripPlannerGraph(config=...)` を構築する順序に変更。`src/graph/graph.py::_main_node_config()` に `policy_db`/`trip_records_db`/`trip_kb_vector_store` の転送も追加 | `tests/integration/test_service_wiring.py` |
| 2 | HITL 承認時、常に `plans[0]` を確定していた（3案から選ぶプロンプトなのに選択を無視） | High | `HitlRefineNode._select_plan_index()` を追加し、サニタイズ後テキストから序数/プラン番号を解釈 | `tests/unit/test_codex_review_fixes.py::TestHitlRefineNodePlanSelection` |
| 3 | `organizer_edited` が `PreProcessNode._handle_feedback` の envelope から欠落していた | Medium | envelope に追加 | `tests/unit/test_codex_review_fixes.py::TestPreProcessNodeOrganizerEditedForwarding` |
| 4 | 幹事応答セッションレジストリ（`_suspended_sessions`）に有効期限・排他制御がなかった | Medium | `Lock` + TTL 付き eviction を追加 | （並行性は自動テスト困難のため実装レビューで確認。§07 運用ガイドに監視項目追記） |
| 5 | `FeedbackUpdateNode._categorize` の LLM 分岐が `pass` のみで実質死にコードだった | Medium | `TripPlannerLLMClient.categorize_complaint()` を実装し、失敗時はキーワードへ縮退 | `tests/unit/test_codex_review_fixes.py::TestFeedbackUpdateNodeLLMCategorization` |
| 6 | 配慮事項の未マッチ節が「other」バケットに入らず消えていた（docstring の記載と実装が不一致） | Low | `_anonymize_care_text` に "other" バケット加算を追加 | `tests/unit/test_codex_review_fixes.py::TestCareTextOtherBucket` |

## Test Execution Summary
- Execution date: 2026-07-29（HITL ガード指摘の対応後。前回 2026-07-06 の実装フェーズのローカル検証・レビュー指摘修正後）
- Total tests: 141（unit 101 / integration 23 / proof_of_boundary 17。内訳は上記表に対応）
- Pass: 141 / Fail: 0 / Skip: 0
- Coverage: TC-01〜TC-11・PB-1〜PB-7・ドメイン固有 D-01〜D-09・レビュー指摘 #1〜#6 すべてカバー
- 2026-07-29 追加分:
  - PB-7 を exact-name ファイルへ統合し 6 ケースに拡張（`hitl_allowed=false` のノード境界／**合成パス**両方、および spy による「同一 state で `hitl_allowed=true` なら `interrupt()` が呼ばれる」対比）
  - `post_process` の 3 状態（selected / pending_organizer / no_candidates）と、プラン projection（`rationale` / `care_accommodations` を出力に載せない）を検証
  - S-3 backstop（ネスト JSON 走査）を `__call__()` 経由で検証: allowlist 値に混入した日本語氏名（敬称付き）・日本語文が続く email を block すること、および走査できない型は fail closed で拒否すること（`set` / 非文字列キー / 循環参照 / 深さ超過 / 未知オブジェクト）。framework の `@final` S-3 はトップレベル文字列のみ走査するため、ネスト JSON はテンプレ側で走査しないと未検査で外に出る
  - `tests/unit/test_framework_compliance_tc06_tc07.py`（scaffold exact-name 必須）を追加。TC-06/07 に加え、本テンプレのノードが `_extra_*` フックで拡張していること・全 `FunctionNode` が `required_trust_level` を宣言していることを検証
- scaffold 同期（2026-07-29）: `scripts/` 配下のゲート自己回帰テスト 143 件も repo ルートで緑（`pytest tests/ scripts/` = 273 passed）。CI の `run-tests` は `tests/` のみを対象とするため、この 143 件はローカル検証で担保する

## 実 LLM 1 回転検証

モックだけで固めた実装は実 LLM を繋いだ瞬間に動かないことがあるため、注入シームへ実 LLM クライアントを刺して 1 回 invoke し、**中身のある出力が出ること**を確認している（`gitproject-dev-flow.md` §14-1-bis）。

| 項目 | 内容 |
|---|---|
| 実施日 | 2026-08-18 |
| 使用 LLM | ローカル CLI（Azure OpenAI `gpt-5.5`）を `BaseLLM` 実装として注入 |
| 注入シーム | `live_llm_injector` |
| 実 LLM 呼び出し | 1 回 |
| 出力 | ['reason', 'prompt'] が非空。縮退理由なし |

本番の LLM は `AnthropicClient` 等で、これは検証用シーム。テンプレの `src/` は変更していない（一時テストを生成→実行→削除する方式）。CI には入れていない（ローカル CLI と鍵が要るため）。
