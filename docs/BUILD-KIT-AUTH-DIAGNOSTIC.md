# BuildKit auth session timeout: 公開前の診断draft

<!-- doc-purpose:start -->
> **役割:** PR11後も報告されたBuildKit timeoutの事実・仮説・次の診断を分離し、未解決として引き継ぐ。
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

**Issueは未作成です。親レビュー後の公開候補であり、解決済みとは扱いません。** 提案titleは「BuildKit docker driver waits on auth session and times out behind corporate proxy」です。会社固有host/IP/appName/proxy値、認証、CA、生logは含めません。

## 報告された状況と未確認の境界

| 項目 | メールで報告された値。独立測定ではない |
| --- | --- |
| OS | Ubuntu 26.04.1 |
| Docker Engine / Compose | 29.9.2 / 5.6.1 |
| BuildKit / Dokploy | 0.34.0 / 0.31.0 |
| builder | docker driver、containerd executor、host builder networking。独立BuildKit containerなしとの報告 |

通常のdocker pullは成功する一方、parallel metadata/auth解決で「fetching auth token from session」「no active session」「only one connection allowed」「DeadlineExceeded」が報告されています。再試行は約60秒、失敗は120/180秒の例があります。対象の公開image familyはapache/spark、apache/superset、library/python、ghcr.io/astral-sh/uvです。これは報告された症状の一般化で、生log転載や原因断定ではありません。

PR11の `BUILDKIT_NO_CLIENT_TOKEN=1` 適用後も再発報告があります。通常pull成功、warm cacheでのbuild成功、local application tagのpull抑制は、この障害の修復証拠にはしません。

## 次の独立診断で埋める記録

1. 実行する各processのCLI/Compose/buildx、Engineに埋め込まれたBuildKit client/serverの正確な版・commitを特定します。報告値をそのまま独立測定欄へ転記しません。Engine/Dokployの採用sourceと実binaryの両方へ照合します。
2. daemon、Dokploy process、builder/client、Dockerfile RUNのどこへproxy/CAが渡るか、値を出さずSET/NOT_SETと信頼経路を記録します。driver=dockerに架空の独立builder container設定を案内しません。
3. 同じ固定FROMだけの合成contextでserial/parallel、warm/cold metadataを比較します。既存cacheやimageは削除せず、隔離した承認済み検証環境を使います。sessionの開始/終了と要求の重なりを確認します。
4. auth session lifetime/concurrency、proxy接続の再利用/切断、clientとdaemonの到達性/CAの差を**仮説**として切り分けます。特定製品やproxyの欠陥と断定しません。
5. raw traceにはtoken/header/endpoint/IDが含まれ得ます。私有記録で検査し、公開物には安全なcode、時刻差、成功/失敗の集計だけを残します。

このdraft作成ではdaemon restart、builder replacement、proxy/CA/security/network変更、auth bypass、実際のbuildを行っていません。operator承認前に実行しません。現時点の確実な結論は、原因と解消条件が未確定という点です。

## 別の中期設計候補

完成imageをCIでbuildしGHCR digestへ固定する方式、registry/Maven cache、公式Engine/BuildKit更新後の再評価は比較候補です。registry到達性、credential、architecture、provenance/signing、distribution、rollbackを評価します。今回のcompute変更では新しいimage公開workflowやregistry資格情報を追加せず、local buildを維持します。

## 公式参照先

- [Compose 5.6.1](https://github.com/docker/compose/releases/tag/v5.6.1)
- [Compose image取得判定の採用版source](https://github.com/docker/compose/blob/v5.6.1/pkg/compose/pull.go)
- [BuildKit upstream](https://github.com/moby/buildkit): 採用Engine内の実commit特定は未実施。session implementationを未確認のまま修復済みとしない
- [Docker daemon proxy](https://docs.docker.com/engine/daemon/proxy/): daemon設定とbuild clientの設定を同一視しない
