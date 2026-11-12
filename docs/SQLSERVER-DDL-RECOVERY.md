# SQL Server DDLの照合順序と途中失敗からの再開

<!-- doc-purpose:start -->
> **役割:** error468・51019の残存診断、exact checkerの保護、CDC部分完了とWindows client別の安全な再開を整理する。
>
> **対象読者:** 基盤構築担当 / 運用担当 / 開発・設計担当
>
> **読むタイミング:** DBA用003が失敗した後や、混在collationでCDCの適用/無効化を再開するとき。
>
> **前提となる資料:** [固定SQL sourceで疑似ERP・MESを検証する](<SQLSERVER-FIXED-DEMO.md>)
>
> **次に読む資料:** [症状から確認する](<TROUBLESHOOTING.md>) / [バージョンと連携互換性](<COMPATIBILITY.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

この資料は固定疑似ERP/MESのDBA用DDLを、安全に診断・再実行する手順です。会社DBへの操作結果を公開する記録ではありません。実際の認証情報・接続先・principal・installation ID/hashはGit外で管理します。[通常導入](SQLSERVER-FIXED-DEMO.md)・[診断SQL](../infra/sqlserver/diagnostics/003_permissions_preflight.sql)

## 1. error 468の条件と修正範囲

catalog側の文字列列と、table variable側の文字列列が異なるcollationを持つと、両方が暗黙collationの比較でerror 468が起こり得ます。再現用の組は`Latin1_General_CI_AS_KS_WS`と`Latin1_General_CI_AS`です。DB/serverのcollationを変更して解決しません。

| 対象 | 監査・修正 |
| --- | --- |
| 003 | `@AllowedGrants.role_name`とprincipal名、permission名同士を両辺`Latin1_General_100_BIN2`。private principalのcase aliasとfixed roleのcase aliasも拒否 |
| 004/005 | capture名のJOIN/EXISTS/scalar比較、schema/table/index/column/role名、双方向EXCEPTの文字列投影を両辺BIN2。CDC手続きの名前付き引数は比較式ではなく元の変数代入を維持 |
| 006 | numeric ID JOIN、catalog列＋固定Unicode literal、既存の両辺BIN2のinstallation比較を監査。同型の異種列比較はなく、SQL本体は変更しない |
| checker v1 | catalog＋固定literalのみ。canonical SQL、003内の埋込定義、runtimeの完全一致契約を変更しない |

binary比較だけへ置き換えてcase aliasを検索対象から消さないよう、CDCの衝突発見ではcatalog解決のobject IDも使用します。同名captureが別sourceに存在する場合も作成前に停止します。ASCII固定名の`UPPER`/BIN2比較はcase aliasを拒否する事前検出にだけ使い、再利用・adoptionや変更対象の一致はBIN2完全一致だけです。許可表やEXCEPTではcaseを含む固定の比較意味を維持し、`DATABASE_DEFAULT`へ環境依存で寄せません。Unicodeの`N'...'`は維持します。

004/005の共有CDC gating role検査では、保存名のBIN2完全一致に加え、`DATABASE_PRINCIPAL_ID(ct.role_name)`と固定roleのprincipal ID一致もORで検出します。case-insensitiveなcatalogで別表記が同じprincipalへ解決される場合を、変更前に51115で拒否するためです。このORは拒否範囲だけを広げます。fixed capture・role owner/kind・member・permissionの認可は完全一致のままで、case-sensitive環境の別principalを同一視しません。名前解決がNULLの場合も既存の完全一致検査や未知object保護を省略しません。

Microsoftの[collation precedence](https://learn.microsoft.com/en-us/sql/t-sql/statements/collation-precedence-transact-sql?view=sql-server-ver16)では、列同士と変数・literalの優先順位を区別しています。006やcheckerのcatalog＋literalを、003の異種列比較と同じ障害と断定しません。

## 2. 途中失敗後はrollbackを推測しない

003と独立診断、004/005は最初に外側transactionを検出し、`XACT_ABORT`を変更せず`RAISERROR`＋`RETURN`で拒否します。`THROW`は継承された`XACT_ABORT ON`を尊重するため、この入口では使いません。[RAISERRORの仕様](https://learn.microsoft.com/en-us/sql/t-sql/language-elements/raiserror-transact-sql?view=sql-server-ver16)に基づく措置です。clientが接続を閉じる場合のrollbackまで阻止する保証ではありません。003は専用sessionを要求し、捕捉できた失敗では自分のtransactionをrollbackしてから、checkerの事前有無・今回作成したか・失敗後の有無とfixed role/permission数を出します。定義やprivate principal名・所有権hashは出しません。診断が失敗しても元のエラーを返します。

これは全エラーのrollback保証ではありません。同じscopeのcompile/recompile error、client取消、接続断などは`TRY/CATCH`の対象外になり得ます。`XACT_ABORT ON`だけでもcompile errorは保証されません。別batch/sessionで作られたobjectまで、後続の失敗が消すとは限りません。**残存原因は、実際のbatch/session・transaction記録がなければ不明**です。特定clientや空白・改行が原因だったと推測で断定しません。[TRY/CATCH](https://learn.microsoft.com/en-us/sql/t-sql/language-elements/try-catch-transact-sql?view=sql-server-ver16)・[XACT_ABORT](https://learn.microsoft.com/en-us/sql/t-sql/statements/set-xact-abort-transact-sql?view=sql-server-ver16)

004/005は共通jobを変更しない事前検査の後、CDC手続きを表単位で実行します。6表を一括rollbackしたとは扱いません。捕捉できた失敗では固定6名について次を示します。

- `preflight_presence` / `current_presence`: present、absent、またはunknown。
- `observed_change`: became_present、became_absent、unchanged、またはunknown。
- mutationを試みたか、元のerror番号、状態を保存して再確認する案内。

これは観測差であり、外部DDLを含めた作成者の証明ではありません。共有job・legacy capture・role・permissionを変更したり、失敗を理由に自動cleanupしたりしません。診断が出なかった場合も、次の独立preflightを使います。

## 3. 独立した読取り専用preflight

1. API/workerのwriterを保留し、別DBA作業と競合しない時間を確保します。
2. 失敗したsessionに未確定transactionがあれば、その状態をprivateに記録し、所有者が状態を確認します。失敗した部分導入を盲目的にCOMMITしません。`XACT_STATE()=-1`ならROLLBACKだけが可能で、変更をcommitできません。このリポジトリの再実行で他sessionのtransactionを操作しません。
3. **新しい専用DBA session**を、承認済み開発DBへ明示接続します。DBA認証は対話入力等の会社標準で扱います。
4. 承認済みcommitのprivate SQLCMD変数と`diagnostics/003_permissions_preflight.sql`を実行します。例のwrapperでは、この診断SQLをprivate folderへbasenameでコピーします。
5. DB/installationの一致、checkerのkind・schema/effective owner・EXECUTE AS dbo・ANSI_NULLS・QUOTED_IDENTIFIER・完全定義、各roleのmember/permission数を確認します。

preflightはSELECTとローカルtable variableの構築だけで、procedure/role/grantを作成・削除・修復しません。戻り値が読取り途中で変わる場合もあるため、同時DDLを止めて実行します。追加のDBA password設定keyは作りません。`site.env`、Dokploy Environment、API、worker、connectorへDBAのpasswordを保存しません。

| 結果 | 次の判断 |
| --- | --- |
| checkerなし、installation一致 | 001/002・既存role/permissionを確認後、承認された003を再実行。006で検証 |
| checkerの定義・全identityが完全一致 | 既存checkerを保持。未完了の限定grant/roleを003が再検査して補う。003成功確認済みなら盲目的な再実行は不要 |
| 51019またはidentity/definition不一致 | writerを止め、既存checkerを保持。DBAがprivateに定義と由来を調査。自動DROP/ALTERやcaller-visible metadataへのfallbackはしない |
| 既存roleの不正permission/member | 旧assetを勝手にREVOKEしない。所有者と別変更として整理 |
| installation不一致・由来不明 | 先へ進まず、別用途のobjectとして保全・調査 |

### checkerを除去してよい条件

完全一致を通らないcheckerを「たぶん自分の失敗作」として削除してはいけません。作成日時や名前だけも証拠にしません。DBAがinstallationの所有権、当該導入の実行記録、完全なprivate定義とsession options、依存先・利用者、保存すべき権限を確認し、**その導入で作った未完了objectである証拠と、限定した除去への承認**が揃った場合だけ、別のreview済みDBA作業として判断します。会社での既存確認・手動処理を、一般的な自動削除へ拡張しません。

証明できない場合は保全し、別途operatorへ判断を戻します。このリポジトリはDROPの実行・生成・強制オプションを提供しません。必要ならprivateな定義/権限backupを取り、限定手順を別reviewにします。CDC historyや業務表を消して回復しません。

### 定義比較は緩和しない

v1の正本は[scripts/sqlserver_safety.sql](../scripts/sqlserver_safety.sql)です。003とpreflightの埋込定義も同じ内容を使用します。DATALENGTHとBIN2完全一致、dbo owner/execution identity、ANSI flagsを個別に検査します。runtimeの既存CRLF取扱いを含む照合契約も変更しません。

今回、whitespaceの一括削除、literal/commentの書換え、字句同値の推定、`CREATE OR ALTER`、新しいextended propertyによる自動adoptionは追加していません。`v1`という固定名とinstallation schema version=1は既存のversion契約で、前回の作成者を証明する永続receiptではありません。将来正本を変更する場合も、旧定義を無言で上書きせず、DBAの明示upgradeとruntimeの対応版を一組でreviewします。

## 4. 確認済み段階から再開する

| 確認できた状態 | 順序 |
| --- | --- |
| 001/002だけ成功、003の結果が不明 | private preflight → 必要な003の再実行 → 006 → 修正版004の承認後に004 → 006 |
| 003と3用途のrole割当まで成功確認済み | まず006。問題がなければ修正版004の承認を待ち、004 → 006。不要なchecker削除や003再実行はしない |
| 004が途中で停止 | fixed captureの現在定義・残存・legacy/jobを確認。一致するものを保持し、同じ修正版004で不足分のみ再開 |
| 明示承認した005が途中で停止 | CDC履歴の必要分を保全済みか確認。残存fixed captureの定義を再検査し、確認付き005だけで続行 |

005は通常導入・通常更新で実行しません。004前は業務6表の`cdc_enabled=0`、fixed captureは0件、004後は業務6表の`cdc_enabled=1`とfixed6件を期待します。legacy件数は導入先で事前記録した値と比較します。特定の既存capture数を公開仕様に固定しません。006は外部DBへ書込まず、controlに既存run/watermarkがある場合もその内容を削除しません。

003成功確認済みという報告は、こちらが006/004の成功やrollback原因を実測した証拠にはしません。導入先で各段階を確認し、失敗時は後続へ進みません。

## 5. Windows・VS Code・sqlcmd

VS Code MSSQL拡張からSQL loginで接続する方法も利用できます。接続先DBを明示し、暗号化はMandatory、`Trust server certificate=true`は自己署名PoCの明示判断に限定します。実運用ではCA/hostname検証へ戻します。Remote SSH先のWindows認証は、その環境のSSPI/Kerberos/資格情報委譲が別途必要です。Windows認証のためにruntimeを管理者化したり、認証方式を自動変更したりしません。

Windowsの`sqlcmd`にはGo版とODBC版があります。まず実行fileと実際の版・helpをprivateに記録します。今回報告されたclientの版は不明です。以下の互換性説明は**Go sqlcmd v1.10.0の公式sourceを読んだ結果**で、Windows実行結果ではありません。

- v1.10.0の`:r`実装は行の残りをfile名として渡し、ダブルクォートを除去しません。引用符がパスの一部になる可能性があります。
- 同版sourceでは引用符なしの行の残りに空白を含められますが、全版/全clientの動作保証にはしません。
- Microsoftの一般的な[sqlcmd command資料](https://learn.microsoft.com/en-us/sql/tools/sqlcmd/sqlcmd-commands?view=sql-server-ver16)は空白入り名の引用を案内します。Goの[版固定実装](https://github.com/microsoft/go-sqlcmd/blob/v1.10.0/pkg/sqlcmd/commands.go)・[IncludeFile](https://github.com/microsoft/go-sqlcmd/blob/v1.10.0/pkg/sqlcmd/sqlcmd.go)と区別して、実際の採用版で確認します。

運用例は`:r`内の絶対パスを避けます。private folderへSQLをbasenameで揃え、そのfolderを作業directoryにします。**外側のPowerShell pathの引用**と、**SQLCMDの`:r`構文の引用**を混同しません。

診断wrapperの例（SQLCMD。実値はprivate変数fileへ）:

```sql
:on error exit
:r dba-variables.sql
:r 003_permissions_preflight.sql
```

適用/検証wrapperは最後のbasenameだけを`003_demo_permissions.sql`または`006_demo_validation.sql`へ替え、別fileとして保存します。004用も別の明示wrapperにし、005を通常wrapperへ並べません。

**PowerShell / Go sqlcmd v1.10.0用の例**

```powershell
# 架空の空白入りpath。実行対象と版は会社承認のprivate値へ置き換える。
$Sqlcmd = (Get-Command sqlcmd -ErrorAction Stop).Source
& $Sqlcmd --version
Push-Location -LiteralPath 'C:\Synthetic SQL Space\private'
try {
    & $Sqlcmd -S '<approved-server>,1433' -d '<approved-database>' `
        -U '<ddl-admin-user>' -N mandatory -C -b `
        -i '.\run-permissions-preflight.sql'
    if ($LASTEXITCODE -ne 0) { throw 'SQL preflight failed; stop here.' }
} finally {
    Pop-Location
}
```

passwordを`-P`やwrapperへ書かず、対話入力します。`-N mandatory`は上記Go版で確認した構文で、採用版helpを再確認します。`-C`は自己署名PoCの判断で、証明書検証を省く影響があります。`strict`や別clientのTLS optionを同じ意味として流用しません。`-i`はPowerShellが解釈する引数で、`:r`の行とは別です。`winget`はここで実行しておらず、導入は会社のソフトウェア配布・承認手順に従います。[版の確認](https://learn.microsoft.com/en-us/sql/tools/sqlcmd/sqlcmd-installed-version)・[Go v1.10.0 TLS option](https://github.com/microsoft/go-sqlcmd/blob/v1.10.0/README.md)

業務DBへの接続・DDL適用の前に、別途許可された隔離合成DBへ採用clientで接続し、`SELECT 1`だけの合成include fileと、空白入り作業directoryで`:r`/`-i`を確認してください。既存SQLを実行してパスのテストをしません。

## 6. 検証と残る受入

[合成統合fixture](../tests/integration/sqlserver_collation/)は専用SQL Server 2022で実行するopt-inです。既定はplanのみ、実行には明示consent・loopback・新規の合成owned DBが必要です。003のchecker作成後に失敗を挿入した**テスト用コピー**、残存proc/role/permissionの観測、明示rollback前後、再実行、無関係checkerの51019、CDCの部分完了とlegacy/job保持を確認します。既存DB・volumeを削除するcleanupは行いません。

ローカルDocker APIが権限拒否された場合は、sudo・別socket・別host・権限変更で回避しません。[専用CI](../.github/workflows/sqlserver-cdc.yml)はGitHubの使い捨てrunner内にSQL Server 2022 DeveloperとSQL Agentを起動し、loopback限定の合成DBへ接続します。企業のsecret・SQL Server・volumeを使いません。Go sqlcmd 1.10.0は公式releaseのSHA-256で検証します。CIの実行結果と対象commitを確認し、未実行・失敗・成功を区別します。SQLFluff/AST/mock/planの成功は実機成功の証明ではありません。Windows clientと企業での受入はこのCIの対象外です。


fixtureの計画だけを確認するコマンド（DB接続・password入力なし）:

**Bash / Zsh共通**

```bash
python3 tests/integration/sqlserver_collation/run.py --plan
```

実行は後日、既に用意された隔離SQL Server 2022 Developer・稼働Agent・公式Go sqlcmd **1.10.0** と、その合成SA認証への許可がある場合だけです。localhostが会社DBへのport forwardでないことをoperatorが確認します。プログラムはclient版を確認してからpasswordを対話入力し、child sqlcmdだけの環境変数へ渡します。継承したSQLCMD起動script/認証やproxy設定を使わず、appのprivate設定を読みません。

以下は**現在実行しないoperator用の例**です。新規合成DBを複数作り、消さずに残します。DB名とowner hashのreceiptをprivateに保管し、後片付けは別の所有者reviewへ回します。実サーバーの準備・port公開・Docker設定変更・DB削除はこのtoolの機能ではありません。

**Bash / Zsh共通**

```bash
python3 tests/integration/sqlserver_collation/run.py \
  --run \
  --consent CREATE_SYNTHETIC_DATABASES_ON_DISPOSABLE_SQL2022 \
  --server tcp:127.0.0.1,1433 \
  --sqlcmd '<official-go-sqlcmd-1.10.0-path>'
```

fixtureの強制列collation差はerror468の再現用で、選択serverの実catalog collation差を測ったものではありません。Agentの確認は共有job ID保持までで、全設定・進捗の認証ではありません。SQL Server 2022のANSI_NULLS OFF負例も未再現です。Goの版検査mockや`:r`のsource読取りを、Windows clientでの実パス試験成功とは記録しません。

追加のrole-alias fixtureは別の新規owned CI/CS DBで、固定6captureと無関係なcaptureを準備します。後者にはCDCの正式手続きで大文字表記のgating roleを指定し、実際の保存名と解決されたprincipalを読み戻します。同じprincipalなら004/005とも51115を期待し、capture/role/member/permission・legacy共有jobが変わらないことを検査します。別principalなら固定6件だけの通常lifecycleと無関係captureの保持を検査します。system metadataをUPDATEして別表記を捏造しません。

出力の`role_alias_coverage`は保存名が実際に異なる同一principalケースと、別principalケースの実行有無を分けます。SQL Serverが保存名を正規化した場合、または選択serverのcatalogで別principalを再現できなかった場合、結果は`passed_with_coverage_gaps`・exit code 3となり、該当ケースの実機受入は未完了です。拒否前後のcapture比較には固定identity/定義列を使い、Agentが非同期更新するLSNやdrop-pending進捗を変更と誤認しません。この追加fixtureも現環境では未実行です。


## CDC初期化の診断

51119は旧004が「独立captureが必要」と誤判定した停止です。修正版004は`has_drop_pending`のNULLを健全候補として扱い、実在するtracked sourceとCTを併せて確認します。51130はpending=1または孤児metadata、51132/51133は独立した共有job検証の失敗です。[読取り専用CDC preflight](../infra/sqlserver/diagnostics/004_cdc_preflight.sql)へprivate SQLCMD変数を読み込み、inventory・LSN・role・index・jobを非公開で確認します。system metadataの直接UPDATEで回避しません。

004はcapture=0/job=0から標準作成、capture=0/job=2から既存job保持、独立したNULL captureとの共存、部分固定captureの不足分作成に対応します。同名captureのschema/table/index/列順/role/net changesが違えば、全体の変更前に停止します。005は履歴削除の別操作なので、最後のcaptureを消して共有jobへ影響しないよう独立した健全captureを引き続き要求します。

## 旧四件の明示置換

この手順はDBA専用です。必要なCDC履歴を先に保全し、connector/consumerを別途停止し、他のDBAによるCDC/source DDL/job操作のないwindowを確保します。旧captureの履歴はdisableにより失われます。既に固定六件へ移行したDBで旧captureを再作成しません。

1. Git外のprivate JSONへ承認した四組を列挙します。各要素は`source_schema`・`source_table`・`capture_instance`だけを持つobjectです。例えば合成fixtureの一組は`{"source_schema":"dbo","source_table":"fixture_old_1","capture_instance":"fixture_old_1_v1"}`です。正確な四組が必須で、wildcard・prefix展開・自動探索はありません。実名を公開文書やPRへ転記しません。
2. `scripts/prepare-sqlserver.py`の`--env-file`と`--output`にGit外のprivate pathを指定し、さらに`--legacy-allowlist`と`--confirm-replace-legacy REPLACE_EXACT_APPROVED_FOUR`を指定します。0600の新規SQLを生成するだけで接続しません。出力は[専用maintenance template](../infra/sqlserver/maintenance/replace_legacy_captures.sql)とそのcommitの004を組み込みます。private JSON/SQLをレビューし、承認済みDBへ専用session・`sqlcmd -b`で実行します。
3. maintenanceは接続DB/CDC、四組の実source identity、全capture inventoryを確認し、固定六件とallowlist以外のcaptureが一件でもあれば51204で変更前に停止します。004のinstallation ownership/version・reader principal・固定全定義・capture health・共有job検証を通してから、不足する固定captureだけを作成します。
4. 固定六件の完全定義とjobを確認した後でだけ、allowlistの残存旧captureを一件ずつautocommitでdisableします。最後に固定六件・指定旧四件の不在・共有jobのID/設定を再検証します。通常004/006には旧capture不在の要件を加えません。

DB全体のCDC、共有job、無関係capture、source表、role、offsetは削除しません。途中失敗時は`phase`・エラー番号・承認entryのordinal・事前/現在の有無を出し、後片付けせず停止します。固定側の部分完了は004の固定六件診断にも残ります。接続/compileエラーで診断自体が出ない場合も、private inventoryを取り直します。同じ承認四組と所有権を保持し、残った状態を確認してから同じ生成SQLで再開します。既に消えた承認entryはskipし、未知のcaptureを追加承認したことにはしません。

実テストの`--initialization`はCI/CS DBの両方で空DB二状態、NULL独立capture、3/6、同名定義不一致、旧四件置換と途中失敗/再実行、未知capture拒否、正式DROP由来の孤児/pending=1、partitioned表拒否を検査します。比較用に合成DBのjobを停止し、実際のLSN/CT履歴・role/grant・job定義・合成offsetの保持を照合します。metadata値の捏造はしません。混在collationの強制error468は実catalog collationの測定とは区別します。
