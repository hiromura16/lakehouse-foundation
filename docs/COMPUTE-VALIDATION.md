# Notebook workspace準備と明示Spark検証

<!-- doc-purpose:start -->
> **役割:** 安全なworkspace plan/apply/probe、既存projectのSpark smoke、結果とpull policyを確認する。
>
> **対象読者:** 基盤構築担当 / 運用担当
>
> **読むタイミング:** computeの配置前、明示検証、proxy build障害の切り分け時。
>
> **前提となる資料:** [サイト設定リファレンス](<CONFIGURATION.md>) / [Dokploy 0.31.0でGitから配置する](<DOKPLOY.md>)
>
> **次に読む資料:** [平時・更新・復旧の運用手順](<OPERATIONS.md>) / [症状から確認する](<TROUBLESHOOTING.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

この手順は、既存projectの配置前チェックと合成3行の互換性検証を扱います。通常DeployではSpark Master/Worker/Historyが常駐し、`spark-volumes-init` は終了0、`spark-check` はreplicas=0でcontainerなしが正常です。検証を開始する前に対象projectと書込み許可を確認します。

## 1. 設定とworkspaceの計画

NOTEBOOKS_DIRだけをNotebookのhost保存先として使い、container内は `/workspace` です。既定の `/srv/lakehouse/notebooks` は配置先の実値ではありません。private設定で外部の専用directoryとWORKER_UID/GIDを選びます。UIDは非root、GIDは数値です。別の保存先キーは導入しません。

**Bash / Zsh共通**

```bash
ENV_FILE="$HOME/.config/lakehouse-foundation/site.env"
./scripts/lakehouse.sh --env-file "$ENV_FILE" notebook-workspace
./scripts/lakehouse.sh --env-file "$ENV_FILE" predeploy
```

最初は変更しません。`exists`、所有者/modeの一致、親の安全性、推定access、create/fix/none/blockedをJSONで確認します。pathやenv全文は出しません。`plan` はCompose構成だけ、`predeploy` は同じworkspace検査も行います。書込みprobeはどちらも未実施です。notebooks無効時は設定・filesystem操作を要求しません。

symlink（祖先・末端・dangling）、`..`、checkout内、filesystem/mount root、home root、private設定領域（選択envファイルの親・CONTROL_SETTINGS_DIRと同一/祖先/子孫になる場所）、通常file、危険な親を拒否します。descriptor/no-followとinode/mount identityで置換を検出し、resolveでsymlinkを通過してから変更する方法は使いません。既存の親やNotebook本文を再帰修正しません。

## 2. operatorが承認した後だけapplyとprobe

**次はhostの所有者/modeを変更し、一時的に書き込む操作です。この変更のoperator承認後だけ実行します。** 所有者変更には必要な権限が要ります。sudoが必要な場合も対象private設定を明示し、configureをrootで新規生成しません。

**Bash / Zsh共通**

```bash
./scripts/lakehouse.sh --env-file "$ENV_FILE" \
  --apply --probe notebook-workspace
```

`--apply` 単独はmetadataの準備のみ、`--probe` は書込み検証を追加する別の同意です。workspace rootをWORKER_UID:WORKER_GID・02770へ揃えます。足りない親は最大3階層だけ0750で作り、既存親は変更しません。2回目が一致すれば不要なfchown/fchmodを実行しません。

LinuxのO_PATH/O_NOFOLLOWとmount metadata、O_TMPFILE|O_EXCLが必要です。probeは意図した非root UID/GIDとcontainer相当のgroup集合で、匿名の排他的な一時inodeを作成・write/read・closeします。既存名へ上書きせず、group書込み可能なdirectoryで他人のfileを誤unlinkする方式へfallbackしません。不要な補助groupを除去できない場合、非対応filesystem、cleanup失敗は拒否です。rootのos.accessを成功証拠にしません。

途中の作成・所有者変更後に失敗した場合、`mutation_attempted` と `partial_or_unknown` を返し、変更なしとは説明しません。作成済みsuffixを残すので、operatorが再inspectします。保護を迂回する再帰chownやroot実行probeは行いません。

CLI管理のupも同じinspectを使います。Dokploy管理ではこの準備を別工程で実施し、その後のDeployだけをDokployへ任せます。Notebookの準備にDokploy installer全体のapplyは不要です。

## 3. 既存projectへSparkチェックを明示する

core、migration、Spark volume initが完了した既存projectを対象にします。別projectを作成したり、欠けたdaemonを起動したりしません。`DATA_VOLUME_PREFIX` がある場合はそれを維持し、実containerのlabelと既存Spark volume名を照合します。storageには元からhealthcheckがなく、preflightはrunning確認です。S3 read/write証拠は後段のIceberg fixtureで確認します。

**Bash / Zsh共通**

```bash
./scripts/lakehouse.sh --env-file "$ENV_FILE" \
  --project-name selected-project spark-check
```

`selected-project` は実際の承認済みappNameへ置き換えます。明示project指定はspark-checkだけで有効です。Composeの `--project-name` とimage/volumeのCOMPOSE_PROJECT_NAME展開を同じ値にし、secret/NO_PROXYはprivate envを優先します。shellのCOMPOSE_PROJECT_NAMEと選択値が不一致なら操作前に停止します。古いshell値は`unset COMPOSE_PROJECT_NAME`で解除します。元のenvは書き換えません。

preflight→common imageのbuild担当 `spark-volumes-init` → `run --rm --no-deps spark-check` の順です。既存coreとMaster/Workerの状態が不正ならbuild前に停止します。container内でも固定Master/Worker JSONと登録Workerの対応を確認し、失敗をlocalへfallbackしません。

実際の処理は **local[2]-with-cluster-readiness** です。Master/Worker到達と登録を確認しても、remote executorがこのjobを実行した証拠ではありません。cluster不要の互換確認だけなら、意図を明示します。ただしcoreと既存の準備済みvolumeは必要です。

**Bash / Zsh共通**

```bash
./scripts/lakehouse.sh --env-file "$ENV_FILE" --local-only spark-check
```

12段階はMaster到達、Worker登録、session、3行fixture、REST catalog、S3書込み、固定Iceberg表、Spark読取り、Trino読取り、値/件数一致、再実行、cleanupです。固定の `compatibility.spark_roundtrip` だけを書き換え、日本語とdecimalを比較します。Spark catalog aliasはlakehouse、Trinoはicebergです。通常Gold/SQL/CDC/Sail領域へ書き込みません。

fixtureは意図して保持し、sessionは停止します。shared spark-work volumeの固定lockで並列書込みを防ぎ、未確認sessionやprocess crashでは非空のRUN_IN_PROGRESS markerを残します。後続は拒否し、operatorが旧container/JVMの停止を確認してからinodeを保持した排他的な保守を行います。lock fileの削除や自動force-clearは行いません。

## 4. 機械可読な結果

stdoutの `LAKEHOUSE_SPARK_RESULT=` で始まる1行がschema_version=1のJSONです。JVM/buildの生出力は公開せず、settings/preflight/build/container起動失敗も安全なstage codeへ変換します。run ID、UTC時刻、全体所要時間、commit/dirty、設定profileとcommand profile、実行mode、12段階、件数、値一致、cleanupを保持します。未実施・不明を成功にしません。

`image_identity=unknown` は実containerを独立に確認できない場合です。`built_image_identity` はbuild直後のlocal tagの観測で、実行containerのimageを保証しません。Iceberg版は固定JARの設定版です。

任意で、既に存在するoperator所有・0700のGit外private directoryへ0600 JSONを原子的に保存できます。directoryを自動作成・権限修正しません。

**Bash / Zsh共通**

```bash
./scripts/lakehouse.sh --env-file "$ENV_FILE" \
  --result-dir /absolute/private/compute-results spark-check
```

結果はrun ID別に一度だけ保存します。上書きとsymlinkを拒否し、50件に達したら自動削除せずoperatorのarchiveを要求します。同じUIDの悪意あるプロセスに対する絶対的な保護は主張しません。部分publish/cleanup失敗の結果は不明として扱います。

host側のbuildは15分、runは10分を上限にします。CLI timeoutだけではJVM停止を証明しません。作成したrun ID・project・service・ownerを再確認できた一時containerだけをID指定でstopし、確認不能ならcleanup=unknown・失敗を返します。常駐serviceのup/down/再作成は行いません。

## 5. local imageのpull方針と残る通信

| build担当: pull_policy=build | 再利用担当: pull_policy=never |
| --- | --- |
| spark-volumes-init | spark-master / spark-worker / spark-history / spark-check |
| demo-api | demo |
| cdc-register | cdc-consumer |
| superset-init | superset |
| worker / airflow / jupyter / mcp | 同じserviceがbuild担当で、別consumerなし |

buildはimageが存在してもbuild手順を実行し、cache再利用とは別です。neverはlocal imageがなければ失敗し、registryから取りません。全体の `--pull never` や公式imageの取得禁止は追加しません。[公式pull_policy](https://docs.docker.com/reference/compose-file/services/#pull_policy)

Compose 5.6.1の公開sourceではimage取得判定の後に選択されたbuild担当をbuildします。depends_onはcontainer起動順でありcold tag解決の実証ではありません。実際のcold/warm起動・cache・registry HEAD抑制は、隔離したdaemon/cacheで別途受入が必要です。cacheを削除して既存環境を試験しません。[Compose 5.6.1 build処理](https://github.com/docker/compose/blob/v5.6.1/pkg/compose/build.go)

Dockerfile FROM、uv、Maven/JARへの通信は残ります。PR11のBuildKit switch適用後もtimeout報告があり、今回のpull policyで解消したとは扱いません。[別の診断draft](BUILD-KIT-AUTH-DIAGNOSTIC.md)

## 6. 受入とrollback

今回のsource試験は合成値・mock・使い捨てLinux fileだけです。実Notebook host権限、root credential drop、Docker cold/warm、Spark/S3/REST/Trino、会社環境は未受入です。現在のsandboxではtrusted-root metadataと補助group制約があり、実child write probe成功を主張しません。

会社適用は承認済みcommit/projectとprivate設定を確認し、workspace plan→承認済みapply/probe→既存管理入口でDeploy→正常one-shot/daemon確認→明示smoke→JSON保管の順です。rollbackは互換版のcode/configへ戻し、Notebook、event/work volume、結果、設定を保持します。過去metadataの記録なしに所有者を一括修復せず、directory/table/volume/image cacheを削除しません。
