# OpenObserveを任意の比較Labとして追加する

<!-- doc-purpose:start -->
> **役割:** 通常のLoki経路を保ち、秘匿済みログを任意のOpenObserve Labへ分岐する設定と未検証範囲を整理する。
>
> **対象読者:** 基盤構築担当 / 運用担当 / 評価・報告担当
>
> **読むタイミング:** ログ比較Labの導入前に、資格情報・保存先・独立停止・受入条件を確認するとき。
>
> **前提となる資料:** [段階的な監視基盤の配置と受入](<OBSERVABILITY.md>) / [Vectorによる対象ログの整形と秘匿化](<VECTOR.md>)
>
> **次に読む資料:** [Lokiの保存・検索・保持期間](<LOKI.md>) / [症状から確認する](<TROUBLESHOOTING.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

この変更は**source・mock・静的検証まで**です。imageのbuild／起動、実API、実配送、Grafana表示、保持による削除、24時間の容量・欠損・重複・性能は未実施です。Dockerへの接続を再試行して受入結果を作ったものではありません。以下の配置操作は、対象hostで別途許可された場合だけ実行します。

## 1. 通常監視との境界

通常の `monitoring` はLoki-onlyです。`openobserve-lab` を選択した時だけVectorが追加設定を読み、redactionの後の同じeventを第二sinkへ渡します。Loki、Prometheus、GrafanaからOpenObserveへの起動依存は追加しません。Lab停止で既存監視を停止させない設計です。[Vectorの配送・損失の境界](VECTOR.md)

```text
対象projectの明示capture → Vector → 秘匿化した共通event
                                   ├─ Loki → 既存Grafana Logs
                                   └─ OpenObserve Lab → OpenObserveのLogs画面
```

OpenObserve自身の指標取得、Prometheus remote_write、metricsの二重配送、Grafanaへの第三者datasource pluginは今回追加しません。HTTP `_json` は**ログ取り込み**のAPIです。UI portをPrometheus互換datasourceへ指定したり、ログtokenを未確認のmetrics経路へ流用したりしません。Phase 2ではprotocol・固定endpoint・token権限・保持・queryの互換性を別に確認します。

## 2. 採用版とimage

OpenObserve OSS **v1.1.2**（2026-10-20）を使います。2026-11-11の公式latestは `v1.2.0-rc1` で、release APIのprerelease flagがfalseでもtag名がrcのため採用しません。公開registryのmanifest照合と実行成功は別です。[正式リリース](https://github.com/openobserve/openobserve/releases/tag/v1.1.2)

[image lock](../infra/openobserve/images.lock.json)には、公式ECRのtag＋index digest、amd64／arm64のmanifest、補助BusyBoxのdigestとbinary hashを記録しています。OpenObserveのvendor binaryとshared libraryは元の公式imageに残し、Rustをbuildしません。

公式tagのruntimeはdistrolessで、shellやcurlを仮定できません。そのため静的BusyBox **1.37.0-uclibc**を公式imageから `/bin/busybox` へCOPYします。amd64／arm64の公開artifactを取得し、ELFにinterpreter／dynamic NEEDEDがないことと必要applet名の存在を静的確認しました。これは派生image内での実行確認ではありません。1.38.0は公式image一覧でunstable系のため、このprobeには使いません。[OpenObserveの公式Dockerfile](https://github.com/openobserve/openobserve/blob/v1.1.2/deploy/build/Dockerfile.tag.amd64)、[BusyBox公式image定義](https://github.com/docker-library/official-images/blob/master/library/busybox)

`/healthz` は固定版sourceにある基本応答で、200＋`status=ok`を返します。containerのprobeは固定loopbackへHTTP接続します。成功しても認証、SQLite、取り込み、検索、保持の削除までの成功を意味しません。[固定版health実装](https://github.com/openobserve/openobserve/blob/v1.1.2/src/api/management/src/request/status/mod.rs)

OpenObserve OSSはAGPL-3.0、BusyBoxはGPL-2.0系です。配布や改変時は各公式ライセンスとソース提供条件を確認します。Enterprise imageやcloud accountを有効化する設定ではありません。[OpenObserve LICENSE](https://github.com/openobserve/openobserve/blob/v1.1.2/LICENSE)、[BusyBox LICENSE](https://git.busybox.net/busybox/tree/LICENSE?h=1_37_0)

## 3. 設定・保存先・資源

Composeは [infra/openobserve/compose.yaml](../infra/openobserve/compose.yaml) を既存coreと同じprojectへ重ねます。既存lifecycle managerから管理し、別名の二重配置を作りません。

| 設定 | 用途 |
| --- | --- |
| `COMPOSE_PROFILES` | 通常はLabを含めない。有効化時は `monitoring,openobserve-lab` を含める |
| `OPENOBSERVE_PORT` | hostのloopback UI/API port。既定5080 |
| `OPENOBSERVE_ROOT_USER_EMAIL` | Lab内のbootstrap管理者ID。外部へのメール送信設定ではない |
| `OPENOBSERVE_ROOT_USER_PASSWORD` | 管理用のprivate秘密値。既存volumeでは保持する |
| `OPENOBSERVE_INGEST_TOKEN_FILE` | Git外・`WORKER_UID`所有0600のJSON file。`token`だけを持つ |
| `WORKER_UID/GID` | VectorとLabの非root実行UID/GID |
| `DATA_VOLUME_PREFIX` | 通常空欄。既存データ名を引き継ぐ場合だけ使用 |

Lab無効時はtoken fileもOpenObserve endpointも必要ありません。Vectorは空の公開placeholderを使用せず、OpenObserve設定を読みません。profile名は**ちょうど `openobserve-lab`**です。似た名前で暗黙有効化しません。

Lab有効時のoffline検証は、root用2項目とtoken fileを確認します。token fileはcheckout外の絶対パスで、各祖先と末尾にsymlinkを許さず、選択した非rootの `WORKER_UID` 所有・**正確に0600**・regular file・1〜4096 bytesを要求します。0400も許可しません。JSONは重複keyのない `{"token":"..."}` だけとし、`o2oi_`＋32英数字のtokenがroot passwordと異なることを確認します。descriptorを固定して4097 bytesまでしか読まず、読取中の変更・差替えも検出します。エラーへ秘密値やprivate pathは含めません。Lab無効時はこのfileを開きません。helperは所有者や権限を修復せず、読取権限がないoperatorも拒否します。

GUIの設定保存からLabを選ぶ場合、token fileはhostの `CONTROL_SETTINGS_DIR`、つまりprivate `site.env` と**同じディレクトリの直下**へ置きます。GUI/APIはそのbasenameだけを既存の `/private` mount内へ対応付けて検証し、設定の保存・返却には元のhost絶対パスを維持します。別のmountは追加しません。このディレクトリ外のtoken fileはhost CLIでは利用できますが、GUIからの読取・保存では拒否します。

`openobserve-data` は専用named volumeで、`<prefix>_openobserve-data` としてSQLite metadata、WAL、ログ保存・cacheを保持します。既存のS3、Iceberg、DB、Grafana、Vectorのvolumeを流用しません。新規volume copy-up用のowner/modeをimage層へ設定し、runtimeは専用rootのUID/GID・0700・symlink不在を検査するだけです。不一致の既存volumeをchown/chmodしたり削除したりはしません。

containerは非root、rootfs readonly、capabilityなし、no-new-privileges、host bindなし、socket・device・host PID/networkなしです。外部公開はせずUI/APIを127.0.0.1へbindします。メモリ上限2GiB・CPU2、tmpfs64MiB、memtable128MiB、DataFusion用256MiBは**構成上限**で、起動／負荷に十分という実測値ではありません。通常monitoringの上限2176MiBへLab分を加算し、OS/cache、disk buffer、spoolを別に見積もります。MemAvailable 3GiB未満やswap継続増加ならLab配置を止め、既存サービスの制限を勝手に変えません。

telemetryとtracingは無効、usage報告はlocalでerror内容の自己報告も無効にします。外部proxyやCAをLabへ自動注入しません。image取得には既存のdaemon／builder設定が別途必要です。通知、SMTP、外部AI、cloud連携は設定しません。

## 4. rootと配送tokenを分ける

Vectorは固定の `POST /api/default/lakehouse_logs/_json` を使用します。Basic user名は `collector`、password側は **`o2oi_`から始まる組織用取り込みtoken**です。採用sourceはこのtokenを通常user照合の前に検査し、取り込みroute以外では拒否します。`collector`をrootメールIDへ置換する必要はありません。[固定版認証実装](https://github.com/openobserve/openobserve/blob/v1.1.2/src/api/common/src/auth/validator.rs)

このtokenはdefault組織内の取り込み用で、単一streamだけへ限定する権限ではありません。UI管理用root password、通常ユーザーpassword、広いAPI権限を持つService Account tokenで代用しません。root passwordと配送tokenは常に別にします。

管理者が明示した場合だけ、helperは `POST /api/default/ingestion-tokens` へ固定名 `lakehouse-vector-lab` のtokenを一度作成し、返却された値をprivate JSONへ0600で保存します。既存fileやsymlinkを上書きせず、既存tokenを自動rotate・削除しません。token名が既存なら停止します。[token作成API](https://github.com/openobserve/openobserve/blob/v1.1.2/src/api/management/src/request/organization/ingestion_tokens.rs)、[token生成と重複拒否](https://github.com/openobserve/openobserve/blob/v1.1.2/src/core/src/ingestion_tokens.rs)

## 5. 初回の二段階有効化

配置先hostの通常operatorが、既存と同じproject・private env・Git版を使います。以下は**配置許可後の例**で、この作業環境では実行していません。秘密値はエディターで確認し、端末や公開ログへ表示しません。現在の管理元がCLIかDokployかを先に確認し、対応する一方の手順だけを使います。

1. `COMPOSE_PROFILES` は既存の `monitoring` を含む値のままとし、まだ `openobserve-lab` を追加しません。root用2項目を用意します。token fileは未作成で構いません。
2. 通常monitoringとlog-spoolの受入を先に行います。[Vector](VECTOR.md)
3. `COMPOSE_PROJECT_NAME` が現在の実appName／projectと一致し、`DATA_VOLUME_PREFIX` 等も既存設定のままであることを確認します。新しいproject名を考えて入力しません。

### CLI管理の配置だけ: 選択した設定からOpenObserveだけを起動

配置先hostの、既存CLI管理を行うoperatorでrepository rootへ移動します。`LAKEHOUSE_ENV_FILE` は既存のprivate `site.env` の絶対パスです。以下は通常CLIと同じ `settings.load` / `environment` を使い、選択設定を子processへ渡します。Composeの9ファイルと `-p` も固定し、古いshell変数による補間を避けます。Dokploy管理のアプリにはこの起動例を使いません。

**Bash / Zsh共通**

```bash
python3 - "$LAKEHOUSE_ENV_FILE" <<'PY'
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "scripts"))
from openobserve_lab import validate_config
from settings import ROOT, environment, load

site = Path(sys.argv[1]).expanduser().resolve()
values = load(site)
profiles = values.get("COMPOSE_PROFILES", "").split(",")
if "monitoring" not in profiles or "openobserve-lab" in profiles:
    raise SystemExit("Keep monitoring enabled and lab disabled for bootstrap.")
validate_config(
    {**values, "COMPOSE_PROFILES": "openobserve-lab"}, require_token=False
)
files = (
    "infra/lakehouse/compose.yaml",
    "infra/compute/compose.yaml",
    "infra/orchestration/compose.yaml",
    "infra/demo/compose.yaml",
    "infra/cdc/compose.yaml",
    "infra/poc/compose.yaml",
    "infra/mcp/compose.yaml",
    "infra/openobserve/compose.yaml",
    "infra/sail/compose.yaml",
)
command = [
    "docker", "compose", "-p", values["COMPOSE_PROJECT_NAME"],
    "--env-file", str(site),
]
child_environment = environment(values)
for relative in files:
    source = ROOT / relative
    command.extend(["-f", str(source)])
    for key in re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", source.read_text()):
        if key not in values:
            child_environment.pop(key, None)
child_environment.pop("COMPOSE_FILE", None)
child_environment.pop("COMPOSE_ENV_FILES", None)
command.extend([
    "--profile", "openobserve-lab", "up", "-d", "--build", "--no-deps",
    "--wait", "--wait-timeout", "180", "openobserve",
])
subprocess.run(command, env=child_environment, cwd=ROOT, check=True)
PY
```

### Dokploy管理の配置だけ: 既存サービスのCommandを一時変更

同じComposeサービスのヘッダーで**現在の実appName**を控えます。サービス自身のEnvironmentでroot用2項目と既存private設定を一致させ、`COMPOSE_PROFILES` はLabを含めず保存します。**Advanced → Run Command → Command** の通常コマンドをprivate記録へ控え、次の一行へ一時変更します。`lakehouse-development-platform-a1b2c3` はその既存appNameへ置換します。9ファイル・既存生成 `.env`・同じ `-p` を使います。[Dokployの設定箇所](DOKPLOY.md#任意composeを重ねる)

```text
compose -p lakehouse-development-platform-a1b2c3 --env-file infra/lakehouse/.env -f infra/lakehouse/compose.yaml -f infra/compute/compose.yaml -f infra/orchestration/compose.yaml -f infra/demo/compose.yaml -f infra/cdc/compose.yaml -f infra/poc/compose.yaml -f infra/mcp/compose.yaml -f infra/openobserve/compose.yaml -f infra/sail/compose.yaml --profile openobserve-lab up -d --build --no-deps --wait --wait-timeout 180 openobserve
```

同じサービスの **Deploy** を実行します。対象はOpenObserveだけで、`--remove-orphans` は指定しません。この段階で通常Vectorを再配置せず、別のComposeサービス、別project、host CLIの `up` / `down` を追加しません。ContainersでOpenObserveがhealthyにならなければそのログを確認し、元のCommandを戻す前に原因を調べます。DokployのCLI実行環境に残る同名変数がないことも確認します。private envやサービスEnvironmentの保存だけで他方が同期されることはありません。

### 共通: health確認とtoken作成

上のいずれかで起動した後、配置先hostで同じ選択設定を使います。以下のhelperは固定loopback APIだけを呼び、Composeの配置管理は行いません。

**Bash / Zsh共通**

```bash
python3 scripts/openobserve_lab.py health \
  --env-file "$LAKEHOUSE_ENV_FILE" --allow-lab
```

この一時段階ではVectorをLab用に再配置しません。OpenObserveのroot bootstrapとUI/APIを確認してからtokenを作ります。

**Bash / Zsh共通**

```bash
python3 scripts/openobserve_lab.py token-create \
  --env-file "$LAKEHOUSE_ENV_FILE" --allow-lab --allow-token-create \
  --output /absolute/private/openobserve-vector.json
```

出力先はcheckout外の絶対パスで、operator所有の既存0700ディレクトリです。tokenを作るoperatorのUIDを選択した `WORKER_UID` と揃えます。GUIから設定する場合は前述の `CONTROL_SETTINGS_DIR` 直下を使います。祖先も含めsymlinkを拒否し、固定したdirectory descriptorから新規fileだけを作成します。helperはroot秘密値をAPI認証へ使いますが、root値や返却tokenをstdoutへ出しません。通信先は選択portの127.0.0.1に固定し、proxy・redirect・任意URLは使いません。応答は1MiBのbyte上限と15秒の単一deadlineを設けて小分けに読み、socket timeoutも残り時間以下へ縮めます。APIが成功した後に保存できなかった可能性がある場合は、自動再試行せず管理UIでtokenを調査します。初回token作成では、明示した `--allow-lab --allow-token-create` により既存token fileを要求せずroot用2項目だけを使います。

4. private設定の `OPENOBSERVE_INGEST_TOKEN_FILE` へ作成先の絶対パスを設定し、`COMPOSE_PROFILES` に `openobserve-lab` を追加します。必要な内部bypassへ `openobserve` を含めます。
5. Dokploy運用ではサービス自身のEnvironmentも一致させます。helperは `site.env` やDokploy Environmentを自動編集しません。
6. CLI管理では次のplanを確認し、同じCLI managerから再配置します。

**Bash / Zsh共通**

```bash
./scripts/lakehouse.sh plan
# ここからは、同じmanagerで配置を許可した場合だけ実行する。
./scripts/lakehouse.sh up
```

Dokploy管理ではhost CLIの `up` を使いません。同じサービスのCommandを、控えた通常の9ファイルCommandへ戻してSaveし、同じサービスのDeployでVectorのfanoutを有効化します。private設定とサービスEnvironmentのLab選択・token pathが一致してから実行します。

token fileはVectorのprivate file backendへreadonlyで渡され、実効権限は所有UIDの0600です。Vectorのconfig renderも秘密を含むため、`docker compose config`全文を公開しません。offに戻す時は通常VectorをLoki-onlyへ再配置し、OpenObserveだけを同managerから停止します。volumeと管理用秘密値は保持します。

## 6. 検索・比較と保持の受入

OpenObserve UIは配置先の `http://localhost:5080/`（port変更時はその値）です。遠隔操作ではSSH転送等を使います。default組織の `lakehouse_logs` を選び、同じ `source_event_id`、normalized level、parser status、秘匿表示、timestampをLokiと照合します。UIへroot認証することと、Vector配送tokenの用途は別です。

helperの `counts` は固定streamのCOUNTとdistinct IDだけを返します。ログ本文、任意SQL、任意streamは出しません。指定windowは1〜3600秒です。

**Bash / Zsh共通**

```bash
python3 scripts/openobserve_lab.py counts \
  --env-file "$LAKEHOUSE_ENV_FILE" --allow-lab --window-seconds 300
```

認証失敗や不正応答、stream未作成を0件へ置き換えません。duplicate_excessは同じIDの差分で、source IDの衝突やwindow内の欠損を解決する値ではありません。単発elapsed_msをp50/p95や24時間の性能として扱いません。[採用版検索API](https://github.com/openobserve/openobserve/tree/v1.1.2/src/api/search)

採用版の時刻受入は、既定で過去5時間・未来24時間です。長時間停止後に古いspoolを再配送する受入では、この範囲外のeventを成功件数へ含めません。HTTP応答の成功だけで全レコードの保存を判定せず、取り込み応答の失敗件数・Vectorの配送指標・実検索のdistinct IDを照合します。保持7日は、取り込み可能な過去範囲とは別の設定です。[採用版の時刻制限](https://github.com/openobserve/openobserve/blob/v1.1.2/src/config/src/config.rs)、[JSON取り込みAPI](https://openobserve.ai/docs/reference/api/ingestion/logs/json/)

保持のglobal初期値は7日です。採用版は正の値で3日未満を拒否します。stream側の設定がglobal値へ優先する場合があるため、UIでも対象streamの保持を確認します。compactorが期限を過ぎたデータを削除するまで遅延があり、7日設定はdisk容量上限や即時削除の保証ではありません。[固定版retention設定・検証](https://github.com/openobserve/openobserve/blob/v1.1.2/src/config/src/config.rs)

| 後日の実機受入 | 現在の状態 |
| --- | --- |
| 派生image build、非root起動、health | 未実行 |
| 実token作成・取り込み成功・検索拒否 | source＋mockのみ。実サーバー未実行 |
| 同じ合成eventのLoki／OpenObserve照合 | 未実行 |
| 秘密値の非表示、認証切替、再配置後の永続化 | mock／静的のみ |
| sink個別停止・buffer飽和・retry・復旧 | 未実行。有界損失設計の説明のみ |
| 24時間件数・欠落・重複、query p50/p95 | 未測定 |
| CPU/RSS/swap・volume使用量、retention削除 | 未測定 |

## 7. 失敗したとき

| 状態 | 観測・判断・戻り先 |
| --- | --- |
| Lab offでcredentialを要求される | profileとVector選択を確認。通常Loki-onlyであるべき |
| state permissionエラー | 既存volumeのUID/GID・modeを私有領域で調査。wrapperは修復しない |
| root bootstrap拒否 | private root設定と既存volumeを照合。passwordを再生成して既存DBへ入れない |
| 配送401/403 | org ingestion tokenの種類・有効状態・default組織、private JSONとVector UIDを確認 |
| token名重複 | 既存tokenの所有者・用途を管理UIで確認。自動削除／rotateしない |
| helperがredirectを拒否 | loopbackの正しいportか確認。root認証を外部URLへ転送しない |
| healthは成功、countは失敗 | healthは到達性だけ。認証・stream・検索APIを別確認 |
| Lab停止時のLoki遅延 | Vectorのsink別buffer・drop・retryを確認。無期限無損失を仮定しない |

OpenObserveを正本の業務データ保存へ昇格させる構成ではありません。通常経路へ戻す際も、他service停止、daemon再起動、volume削除、prune、host security変更を自動実行しません。
