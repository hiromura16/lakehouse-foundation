# BashとZshの両方で手順を実行する

<!-- doc-purpose:start -->
> **役割:** 会社VMのZshとBashでコマンドを使い分け、対話入力・コメント・shebang・引用の違いを確認する。
>
> **対象読者:** 初めて使う人 / 基盤構築担当 / 開発・設計担当
>
> **読むタイミング:** Zsh端末で初回構築や運用のコマンドを貼る前、シェル例を追加・変更するとき。
>
> **前提となる資料:** [Linuxサーバーを最初に準備する](<HOST-SETUP.md>)
>
> **次に読む資料:** [初めて構築する人の一本道ガイド](<START-HERE.md>) / [変更する人のための読みやすさと検証規約](<../CONTRIBUTING.md>)
>
> **文書一覧:** [目的・読者から探す文書マニフェスト](<DOCUMENTATION-MANIFEST.md>)
<!-- doc-purpose:end -->

会社VMのログインシェルがZshでも、このリポジトリの配置・素材投入・保守コマンドを実行できます。共通の例は **Bash / Zsh共通**、入力方法が違う例は **Bash** と **Zsh** を分けて示します。シェルを変更する作業や、既存の設定を初期化する操作は不要です。

## 1. 今使っているシェルを確認する

`SHELL`は設定されたログインシェル、`ps`は今の端末のプロセスを確認するために使います。親のBashからZshを起動した場合などは、両者が異なることがあります。

**Bash / Zsh共通**

```bash
printf 'Configured login shell: %s\n' "$SHELL"
ps -p $$ -o comm=
```

Zshは `zsh --version`、Bashは `bash --version` でも版を確認できます。検証はZsh 5.9とBashのクリーンなプロセスで行い、個人のalias・pluginに依存しない例にします。

## 2. Zshの対話端末ではコメントを有効にする

Zshの対話シェルでは、設定によって `#` から始まる行がコメントとして扱われません。手順のコメント付きブロックを貼り付ける前に、Zshだけで次を一度実行します。Bashでは実行しません。

**Zsh**

```zsh
setopt INTERACTIVE_COMMENTS
```

この設定は今のZshへ適用します。新しい端末でも必要な場合は、会社のシェル設定管理に沿って `.zshrc` へ同じ設定を組み込みます。この手順は既存ファイルへ自動追記しません。

以降の **Bash / Zsh共通** ブロックは、同じ内容を使います。コードフェンスの `bash` 表記は構文表示にも使っているため、実行できるシェルは直前のラベルを確認します。

## 3. 対話入力は書き方を選ぶ

Bashの `read -p` は入力前の表示文を指定します。Zshの `read -p` はcoprocessから読む指定なので、同じ意味にはなりません。Zshでは変数名と表示文を `NAME?表示文` の形で引用します。

**Bash**

```bash
read -r -p '確認用の値: ' SHELL_DEMO_VALUE
printf '入力した値: %s\n' "$SHELL_DEMO_VALUE"
```

**Zsh**

```zsh
read -r 'SHELL_DEMO_VALUE?確認用の値: '
printf '入力した値: %s\n' "$SHELL_DEMO_VALUE"
```

`-r`は入力中のbackslashを保持します。以降の変数参照は二重引用符で囲み、空白や `[]` を含む値が別の引数へ分割されたり、globとして解釈されたりしないようにします。passwordはこの確認例へ入力せず、専用のprivate設定や各GUIの資格情報欄へ入れます。

実手順では [SSH転送](START-HERE.md)、[Dockerのpackage版選択](HOST-SETUP.md)、[Sambaの許可CIDR](COMPANY-ROLLOUT.md)、[素材の保存先](DEMO-DATA.md) に両方の例があります。

## 4. ログインシェルとスクリプトの実行先を分ける

ログインシェルは端末へ入力したコマンドを解釈します。`./scripts/lakehouse.sh` のように実行したファイルは、先頭のshebangに従います。このリポジトリのBash wrapperはZsh端末からもBashで動きます。

**Bash / Zsh共通**

```bash
./scripts/lakehouse.sh --help
bash infra/dokploy/install.sh --help
python3 scripts/configure.py --help
```

`.sh`だからといって `zsh scripts/lakehouse.sh` や `source scripts/lakehouse.sh` で読み直しません。Bash専用の処理をZshで再解釈することになります。`sudo bash infra/dokploy/install.sh ...` も、Zsh端末から実行できる明示的なBash呼出しです。

`site.env`はシェルスクリプトではありません。BashでもZshでも `source site.env` は使わず、`LAKEHOUSE_ENV_FILE` / `--env-file` から専用の読取処理へ渡します。

## 5. 共通で使う変数・関数・uv環境

環境変数と引用付き引数は、次の形を両方で使えます。`export`は今の端末と子プロセスへ適用され、別の端末へは自動同期しません。

**Bash / Zsh共通**

```bash
export LAKEHOUSE_ENV_FILE="$HOME/.config/lakehouse-foundation/site.env"
./scripts/lakehouse.sh plan
```

venvをactivateすることは必須ではありません。各手順にある専用venvのPythonへの絶対パスを、そのまま両シェルから実行できます。稼働アプリとは別のuv環境とlockを維持します。

保守関数 `lakehouse_compose()` / `dc()` はBashとZshで使える定義です。引用した `"$@"` で引数の境界を保ち、標準入力を子プロセスへ渡します。定義は開いている端末の中だけに存在するため、新しい端末では該当ガイドの準備を再実行します。

## 6. Zsh固有の挙動でつまずいた場合

| 症状 | 原因と対応 |
| --- | --- |
| `command not found: #` | 対話端末のコメントが無効。先に `setopt INTERACTIVE_COMMENTS` |
| `read: -p: no coprocess` | Bash版の入力例を使っている。直下のZsh版へ切り替える |
| `no matches found` | 未引用の `[]` / `*` / `?` がglobになった。値やpathを引用する |
| `read-only variable: status` | `status`はZshの特別な変数。自作変数には用途が分かる別名を使う |
| 保存先に空白があると引数がずれる | `"$VARIABLE"` / `"$@"` を維持。引用を削除しない |
| 一覧の値をforへ渡すと1要素になる | ZshのscalarはBashと同じ暗黙分割をしない。提示した配列・Python処理を維持 |
| 別端末で関数が見つからない | 関数やexportはその端末の状態。必要な準備を再実行 |
| `permission denied` | シェル変更だけではDocker権限・file権限は変わらない。ホスト準備へ戻る |

`status`、`path`、`fpath`等を独自の作業変数にしません。`NOMATCH`や全体のword splittingを無効化して手順の不備を隠す方法にも依存しません。引用・明示引数・専用Python処理で同じ意味になる例を使います。

## 7. 手順を変更する人の確認

shellのコード例には実行対象をラベルで示し、共通なら両方、専用なら対応するshellで構文検査します。実行検査は入力・引数・stdin・shebang等の最小の安全なstubで行い、文書検査からsudo・SSH・Deploy・実DB接続を起動しません。

**Bash / Zsh共通**

```bash
python3 scripts/shell_examples.py --check
python3 scripts/documentation.py --check
```

構文検査だけでは、接続先・権限・業務結果の正しさは保証しません。会社での実行は、それぞれの手順の成功条件と [評価計画](reports/EVALUATION-PLAN.md) で確認します。新しいNotionの説明原稿にも同じシェル表記を使います。

## 公式資料

- [Zsh builtin read](https://zsh.sourceforge.io/Doc/Release/Shell-Builtin-Commands.html#index-read)
- [Zsh optionsとINTERACTIVE_COMMENTS](https://zsh.sourceforge.io/Doc/Release/Options.html)
- [Zsh parametersと特別な変数](https://zsh.sourceforge.io/Doc/Release/Parameters.html)
- [GNU Bash builtins](https://www.gnu.org/software/bash/manual/html_node/Bash-Builtins.html)
