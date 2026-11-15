# Studioからサービスを起動・再起動・停止する

<!-- doc-purpose:start -->
> **役割:** profile設定と実稼働を区別し、管理者承認済みの固定サービスをGUIから安全に操作する。
>
> **対象読者:** 初めて使う人 / 運用担当 / 開発・設計担当
>
> **読むタイミング:** Studioのサービス操作を利用するときと、host runnerの承認・接続・結果未確認からの復旧を行うとき。
>
> **前提となる資料:** [React版 Lakehouse Studioを利用する](<REACT-PORTAL.md>) / [サイト設定リファレンス](<CONFIGURATION.md>)
>
> **次に読む資料:** [GUI操作APIとSwagger](<API.md>) / [平時・更新・復旧の運用手順](<OPERATIONS.md>) / [症状から確認する](<TROUBLESHOOTING.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

React版Studioの**サービス**画面に、用途別グループの操作ボタンを追加しています。
これは、準備済みのPoCを管理するための機能です。Web APIは実行要求を保存し、
ホストの管理者が明示的に起動したforeground runnerだけがDockerを操作します。
runner未接続ではボタンを無効にし、HTTPの到達確認をDockerの稼働確認と混同しません。

## 画面の表示を読む

| 表示 | 意味 |
| --- | --- |
| profile未選択／起動対象の設定が未選択 | API起動時のprofileに含まれていない。コンテナ停止の証拠ではない |
| 実コンテナ：稼働中 | 接続中runnerが、同じprojectの対象コンテナを最近確認した |
| 実コンテナ：停止中 | runnerが対象グループの停止を確認した |
| 実コンテナ：未確認 | runner未接続、観測が古い、または状態を確定できない |
| 管理機能未接続 | foreground runnerが未起動、停止、または認証・確認に失敗している |
| 受付済み | 要求を保存した段階。コンテナの起動完了ではない |
| 結果未確認 | 実行中の切断・期限切れ・部分失敗等。確認なしに再実行しない |

保存したprofile、API起動時のprofile、runnerの観測は別々に扱います。
旧checkoutの公開設定ファイルは、固定パス・内容一致・安全な親・readonlyを確認した
場合に限り扱います。Vectorの公開された空token用exampleファイルも対象ですが、
実際のprivate tokenは対象外です。token設定キー自体の秘密分類と0600保存は維持します。
起動成功後のprofile保存だけでAPIの起動時設定を書き換えません。
外部URLが空のサービスは、起動してもlocalhostへfallbackしません。
SailはRPCサービスなので、操作欄はありますが架空のWeb画面リンクは作りません。

## 操作の手順

1. **サービス**画面で対象の**起動**、**再起動**、**停止**を選びます。
2. 確認ダイアログで、影響する全コンテナと追加profileを確認します。
   Grafana等のボタンは、単一コンテナではなく下表のグループを操作します。
3. 同意欄を選択して確定します。確認内容の有効期限は60秒です。
4. 操作IDと状態を確認し、`succeeded`まで待ちます。画面を更新しても
   直近の操作を照合します。応答が失われた場合は同じidempotency keyをGETで
   照合し、POSTの自動再送は行いません。
5. 起動後のnative画面のログイン・業務動作・外部経路は別途確認します。

停止はvolume削除、DB削除、ファイル削除を伴いません。初回のイメージ取得・build、
SQL初期化、Notebook領域の準備、CDC設定、Cloudflare設定、Spark smokeやSailの
ベンチマークを、このボタンから暗黙に実行しません。

## 操作できる範囲

| グループ | 起動時の固定対象 | 再起動・停止の対象／条件 |
| --- | --- | --- |
| monitoring | prometheus、grafana、loki、vector | 同じ4サービス。OpenObserve稼働中の停止は拒否 |
| automation | n8n-db、n8n | n8n。DBは保持 |
| bi | superset-db、superset-redis、superset | superset。初期化成功済みが必要。DB/Redisは保持 |
| compute | spark-volumes-init、spark-master、spark-worker、spark-history | Master/Worker/History。Notebook・Sailとの共有依存を保護 |
| notebooks | spark-volumes-init、jupyter | jupyter。準備済みのworkspaceが必要 |
| openobserve-lab | openobserve | openobserve。Vectorの送信先切替も影響対象として扱う |
| sail-lab | Spark初期化と3daemon、sail-server | sail-server。Spark基盤は保持 |

coreのDB・storage・catalog・trino・worker、操作APIとポータル自身は停止対象に
しません。CDCと外部Source接続、host監視の権限追加、未実装のAirflow・Qdrant・
Marquez等は、この初期版のrunnerから操作しません。GUIで理由を表示します。
ホスト全体のDocker管理画面や、任意コマンド実行APIではありません。

追加のprivate overlayが標準以外のbind mountを増やす場合は承認を拒否します。
たとえば外部公開用の独自Superset Python wrapperは、この初期版のallowlistに
含めません。既存wrapperを外して承認を通すことはせず、BIを承認groupから外して
既存managerで管理します。新しいmount契約は別途、ソースレビューと回帰検証が必要です。

同時操作は1件です。投入・pipeline等の実行中job、workerのbusy、必要設定、
共有依存、初期化状態、イメージの有無、Labの資源余力を確認し、不明なら拒否します。
Labのメモリー判定は開始時の余力検査であり、実稼働の消費量や性能の保証ではありません。

## 管理者の準備と承認

Reactの**セットアップ**画面は、固定10機能の不足設定・依存profile・初期化・検証手順を
一か所に表示し、operator向けの非実行JSON計画を保存できます。初回導入をGUIから
実行する権限は付けません。準備と初回配置の後、この節に従って対応groupを承認します。
sourceや設定の更新は既存runnerの承認を失効させます。進行中操作を確認してから、
新しいauthority先で再承認します。旧queueや承認を消して拒否を解除しません。

既存の管理方法を確認します。**このrunnerはoperator管理のCompose専用**です。
Dokployが同じprojectのライフサイクルを管理している場合は併用せず、Dokployから
操作します。新しいWeb/host port、Docker socket mount、sudoers、常駐rootサービスを
追加する必要はありません。

先に、この版のAPIとReact画面を通常の配置手順で反映し、既存coreのhealthを
確認します。この反映そのものを未接続の管理ボタンから行うことはできません。
各グループのimage、必要secret、DB bootstrap、既存volume、Notebook workspaceを
既存の正式手順で準備してから承認します。Web/APIへ管理者用DB資格情報は渡しません。
この初期版は、対象グループのコンテナが一度正式な手順で作成済みであることも
確認します。完全に未導入のprofileは`INITIAL_SETUP_REQUIRED`として拒否します。
したがって、初回インストールをGUIボタンだけで完結させる機能ではありません。
停止済み・準備済みのグループの再開が対象です。

承認先は、管理者本人が所有する**既存の空の0700ディレクトリ**を指定します。
CLIは親ディレクトリを作成・修復しません。新規作成が必要な場合だけ、管理者が
ホストへの作成を承認して次を実行します。既存名なら`mkdir`は失敗するので、
内容を確認せず上書きやchmodで修復しません。

**Bash / Zsh共通**

```bash
AUTHORITY_DIR="$HOME/lakehouse-service-authority-poc-a"
mkdir -m 0700 "$AUTHORITY_DIR"
```

次の例はBash/Zsh共通です。private設定とprojectとportは、既存PoCに合わせて
管理者が選びます。`OPERATOR_PYTHON`には[uvで作成した品質環境](../CONTRIBUTING.md#専用uv環境で検査する)
等のPythonを指定します。ホストrunner本体は標準libraryと同梱moduleを使います。

**Bash / Zsh共通**

```bash
ENV_FILE="$HOME/.config/lakehouse-foundation/site.env"
AUTHORITY_FILE="$AUTHORITY_DIR/approval.json"
OPERATOR_PYTHON="$HOME/.local/share/lakehouse-foundation/source-quality-venv/bin/python"

"$OPERATOR_PYTHON" scripts/lifecycle_operator.py \
  --env-file "$ENV_FILE" --manifest "$AUTHORITY_FILE" \
  --project poc-a --api-port 8502 --groups monitoring,automation
```

フラグなしはread-only planです。既存のDocker接続権限が必要ですが、コンテナ、
承認ファイル、queueを作成・変更しません。出力は固定状態とdigestで、env全体、
コマンドの出力、認証値を表示しません。読み取りに失敗した場合も安全側で停止します。
既存sudoが必要なら管理者の端末で認証し、以降の各コマンドへ`--sudo`を付けます。
これは既存の非対話sudoを使う選択であり、権限やsudo設定の変更ではありません。

planの対象と管理方法を確認し、ホストでの承認ファイル作成を許可した後に実行します。

**Bash / Zsh共通**

```bash
"$OPERATOR_PYTHON" scripts/lifecycle_operator.py \
  --env-file "$ENV_FILE" --manifest "$AUTHORITY_FILE" \
  --project poc-a --api-port 8502 --groups monitoring,automation --approve
```

承認はWebから書き換えられない専用dirへ、0600のmanifestと解決済みComposeの
private snapshotを保存します。snapshotにはsecretが含まれるためGit・Issue・ログへ
添付しません。**CONTROL_SETTINGS_DIR全体の外、checkoutの外**を選びます。
Webから書き込めるenv/overlayを後からrootで再解釈する方式にはしません。
実行権限の正本は固定snapshotで、元ファイルのdigest確認は変更時の失効検出です。
承認時の解決済み正本も`rendered-source.json`として0600で保存します。
承認後の再検査では元のenvやoverlayをComposeで解析し直しません。
このファイルを持たない旧manifestは使用できず、別の新規承認先で再承認します。
元設定・ソース・overlayを更新した場合は、停止・状態確認後に別の承認先で再承認します。
適用済みnative製品のprivate overlayは、各コマンドへ同じ`--overlay`を明示します。

Docker操作の実行を許可した後、同じ端末でforeground runnerを起動します。

**Bash / Zsh共通**

```bash
"$OPERATOR_PYTHON" scripts/lifecycle_operator.py \
  --env-file "$ENV_FILE" --manifest "$AUTHORITY_FILE" \
  --project poc-a --api-port 8502 --groups monitoring,automation --run
```

端末を閉じるかCtrl+Cでrunnerは停止します。以後GUIの操作は無効になります。
実行途中に停止した場合は結果未確認として残し、自動で要求を再実行しません。
sudoの有効期限切れも権限変更で回避せず、結果確認と端末側の再認証を行います。

### 信頼境界と運用上の制限

- Web APIはDocker socket、sudo、Dokploy管理鍵を持ちません。owner側CONTROL認証と
  runner専用tokenを分け、Web BFFはrunner専用routeを転送しません。
- host側は固定project、loopbackのAPI port、Compose label、owner label、private mountを
  確認してからtokenを読みます。shellのDocker context/proxy等で別daemonへ送りません。
- 操作対象は同梱allowlistと管理者の承認範囲の積です。APIからpath、service名一覧、
  container ID、任意Compose引数やshellは指定できません。
- `DATA_VOLUME_PREFIX`は空または同じprojectに限定します。既存のvolume所属labelと
  保存先mountが一致しない場合は拒否します。別PoCのvolume採用やNotebook保存先の
  移管は、このGUIで行う操作と分けて既存managerで確認します。
- ownerの共通ログインは個人別RBACではありません。Cloudflareのメール制限と
  製品ログインを引き続き使います。操作履歴は個人を証明する監査ログではありません。
- OpenObserve有効／無効では別の承認済みVector構成を使います。profileを外しただけで
  sink定義が消えるとは仮定しません。長時間の運転・障害試験は別の受入です。

## APIと結果未確認からの復旧

owner操作は既存Bearer認証を要求します。Reactは既存cookie/CSRF付きBFFを通り、
ブラウザーへCONTROL_API_TOKENやrunner tokenを返しません。

| API | 用途 |
| --- | --- |
| `GET /lifecycle/capabilities` | 接続状態、許可操作、観測、保存済み／API起動時profile |
| `POST /lifecycle/plans` | 固定service/actionの影響確認。実行しない |
| `POST /lifecycle/operations` | plan・確認値・idempotency keyで1件受付 |
| `GET /lifecycle/operations/by-key/{key}` | 応答を失った受付の照合 |
| `GET /lifecycle/operations/{id}` | queued、claimed、running、succeeded、failed、unknown |
| `GET /lifecycle/operations` | 最新履歴。最大20件 |

queueは専用private dirの`.lifecycle`へ原子的に保存し、最大100件の操作、50件の
確認plan、上限付きauditを保持します。既存`/jobs`の非永続契約は変更しません。
runnerのheartbeatは30秒で期限切れ、実行leaseは300秒で結果未確認へ移ります。
接続の`last_seen`と、各サービスの観測時刻は別です。runnerは走査開始時刻を
`observed_at`として送り、heartbeatだけでは観測を新しくしません。観測が40秒を
超えた場合や時刻が無い場合は、担当が接続中でも実コンテナは「未確認」です。
一件の操作結果で他サービスの観測時刻を更新せず、次の走査を待ちます。
heartbeatだけで実行leaseを延長しません。各Docker変更の直前にrunner専用の
permitで同じclaimを再検査し、期限切れ・別runner・結果未確認なら開始しません。
期限切れやAPI再起動をコンテナの成功と解釈しません。

Dockerの`restart`は環境変数の更新を反映する操作ではありません。
設定変更時は新しい承認と通常の再配置を使います。Composeの`up --no-build`、
`--no-deps`、`--wait`の意味は[公式upリファレンス](https://docs.docker.com/reference/cli/docker/compose/up/)、
再起動の境界は[公式restartリファレンス](https://docs.docker.com/reference/cli/docker/compose/restart/)を参照します。

`unknown`の場合は、管理者が同じprojectの状態と部分変更を確認します。
進行中コマンドがないことを確認してから、操作IDを指定して明示的に照合を終了します。
次は**再実行コマンドではありません**。未確認の記録を管理者確認済み失敗へ移します。

**Bash / Zsh共通**

```bash
"$OPERATOR_PYTHON" scripts/lifecycle_operator.py \
  --env-file "$ENV_FILE" --manifest "$AUTHORITY_FILE" \
  --project poc-a --api-port 8502 --groups monitoring,automation \
  --reconcile '0123456789abcdef01234567'
```

必要な復旧を通常の管理方法で行った後、GUIで新しいplanを確認します。
queue/tokenやvolumeを削除して未確認状態を消すことはしません。

## 検証とロールバック

合成ASGI・永続queue・固定argv・権限境界・React通信/SSRのテストと、実Docker起動、
実ブラウザー、会社受入を区別します。ソーステストだけでDockerの起動成功、
外部Access認証、Notebook権限、24時間稼働や製品互換性を合格扱いにしません。

初回の実受入は別途許可されたテストPoCで行います。runner未接続時の拒否、
起動→再起動→停止、共有依存の拒否、jobs実行中の拒否、通信切断、API/runner再起動、
unknownの手動確認、volume・既存DB・ノートブック保持を確認します。
本番・会社DB・既存別projectへの接続や書込みは受入範囲に含めません。

戻すときはrunnerを停止し、進行中の操作を確認してから、既存managerで承認済みの
API/画面imageへ戻します。操作で起動したサービスは、必要性を確認して同じmanagerで
停止します。volume、データ、private設定、承認snapshotと履歴は保持します。
`down -v`、prune、再帰的なchmod/chown、ホストのセキュリティ変更は復旧手順にしません。
