// Viteのraw読み込み。画面ファイルは文字列としてWorkerへ同梱される。
declare module "*?raw" {
  const content: string;
  export default content;
}
