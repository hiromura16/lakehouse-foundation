"use strict";
const byId = (id) => document.getElementById(id);
const sourceForm = byId("source-form");
const contractForm = byId("contract-form");
let sourceRevision;
let contractRevision = 0;
let sources = [];
function node(tag, text, className) {
  const item = document.createElement(tag);
  if (text !== undefined) item.textContent = text;
  if (className) item.className = className;
  return item;
}
function notify(message, error = false) {
  const item = byId("notice");
  item.textContent = message;
  item.dataset.error = String(error);
  item.hidden = false;
}
async function api(path, method = "GET", body) {
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: body
      ? { "Content-Type": "application/json", "X-Lakehouse-Request": "1" }
      : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.headers.get("content-type")?.includes("application/json")) {
    throw new Error("ログイン状態を確認し、ページを再読込してください。");
  }
  const result = await response.json();
  if (!response.ok)
    throw new Error(
      result.message +
        (result.requestId ? "（操作ID: " + result.requestId + "）" : ""),
    );
  return result;
}
function field(form, name) {
  return form.elements.namedItem(name);
}
function fill(form, values) {
  for (const [key, value] of Object.entries(values)) {
    const input = field(form, key);
    if (input) input.value = String(value);
  }
}
function clearSource() {
  sourceForm.reset();
  sourceRevision = undefined;
  field(sourceForm, "id").disabled = false;
  byId("source-heading").textContent = "ソースを登録";
}
function clearContract() {
  contractForm.reset();
  contractRevision = 0;
  for (const key of ["id", "sourceId", "datasetId"])
    field(contractForm, key).disabled = false;
  byId("contract-heading").textContent = "契約の下書きを作成";
  byId("columns").replaceChildren();
  addColumn();
}
function choices(options, value) {
  const select = node("select");
  for (const [key, label] of options) {
    const option = node("option", label);
    option.value = key;
    select.append(option);
  }
  select.value = value;
  return select;
}
function labeled(text, input) {
  const label = node("label", text);
  label.append(input);
  return label;
}
function addColumn(
  value = {
    name: "",
    dataType: "string",
    classification: "internal",
    primaryKey: false,
  },
) {
  if (byId("columns").children.length >= 50)
    return notify("列は50列までです。", true);
  const row = node("div", undefined, "column");
  const name = node("input");
  name.required = true;
  name.maxLength = 64;
  name.pattern = "[a-z][a-z0-9_-]{0,63}";
  name.value = value.name;
  name.dataset.field = "name";
  row.append(labeled("列の論理名", name));
  const pair = node("div", undefined, "column-row");
  const type = choices(
    [
      ["string", "文字列"],
      ["int64", "整数"],
      ["decimal", "小数"],
      ["boolean", "真偽値"],
      ["date", "日付"],
      ["timestamp", "日時"],
    ],
    value.dataType,
  );
  type.dataset.field = "dataType";
  const classification = choices(
    [
      ["public", "公開"],
      ["internal", "内部"],
      ["confidential", "機密"],
    ],
    value.classification,
  );
  classification.dataset.field = "classification";
  pair.append(labeled("型", type), labeled("分類", classification));
  row.append(pair);
  const key = node("input");
  key.type = "checkbox";
  key.checked = value.primaryKey;
  key.dataset.field = "primaryKey";
  const keyLabel = labeled("主キーに含める", key);
  keyLabel.className = "check";
  row.append(keyLabel);
  const remove = node("button", "この列を削除", "secondary");
  remove.type = "button";
  remove.onclick = () => row.remove();
  row.append(remove);
  byId("columns").append(row);
}
function empty(container, message) {
  container.replaceChildren(node("div", message, "empty"));
}
async function refresh() {
  const [sourceData, contractData, status, audit] = await Promise.all([
    api("/api/sources"),
    api("/api/contracts"),
    api("/api/status"),
    api("/api/audit"),
  ]);
  sources = sourceData.items;
  const sourceList = byId("source-list");
  sourceList.replaceChildren();
  if (!sources.length)
    empty(
      sourceList,
      "まだソースがありません。右のフォームから論理名を登録してください。",
    );
  for (const source of sources) {
    const card = node("article", undefined, "card");
    card.append(
      node("h3", source.displayName),
      node("span", source.status === "draft" ? "下書き" : "保留", "tag"),
      node("p", source.id + " · " + source.kind),
      node("p", "担当: " + source.ownerTeam + " / 第" + source.revision + "版"),
    );
    const edit = node("button", "定義を編集", "secondary");
    edit.type = "button";
    edit.onclick = () => {
      clearSource();
      fill(sourceForm, source);
      sourceRevision = source.revision;
      field(sourceForm, "id").disabled = true;
      byId("source-heading").textContent =
        "ソースを編集（第" + source.revision + "版）";
      sourceForm.scrollIntoView({ block: "nearest" });
    };
    card.append(edit);
    sourceList.append(card);
  }
  const sourceSelect = field(contractForm, "sourceId");
  const selected = sourceSelect.value;
  sourceSelect.replaceChildren();
  for (const source of sources.filter((item) => item.status === "draft")) {
    const option = node("option", source.displayName + "（" + source.id + "）");
    option.value = source.id;
    sourceSelect.append(option);
  }
  if ([...sourceSelect.options].some((option) => option.value === selected))
    sourceSelect.value = selected;
  const contractList = byId("contract-list");
  contractList.replaceChildren();
  if (!contractData.items.length)
    empty(
      contractList,
      "契約はまだありません。ソースを登録してから、列と主キーを定義してください。",
    );
  for (const contract of contractData.items) {
    const card = node("article", undefined, "card");
    card.append(
      node("h3", contract.id),
      node("span", "下書き・未適用", "tag"),
      node("p", contract.sourceId + " / " + contract.datasetId),
      node(
        "p",
        "第" +
          contract.revision +
          "版 · " +
          contract.columns.length +
          "列 · 鮮度 " +
          contract.freshnessMinutes +
          "分",
      ),
    );
    const edit = node("button", "次の版を作成", "secondary");
    edit.type = "button";
    edit.onclick = () => {
      clearContract();
      fill(contractForm, contract);
      contractRevision = contract.revision;
      for (const key of ["id", "sourceId", "datasetId"])
        field(contractForm, key).disabled = true;
      byId("columns").replaceChildren();
      contract.columns.forEach(addColumn);
      byId("contract-heading").textContent =
        "契約の第" + (contract.revision + 1) + "版を作成";
      contractForm.scrollIntoView({ block: "nearest" });
    };
    card.append(edit);
    contractList.append(card);
  }
  const services = byId("service-status");
  services.replaceChildren();
  for (const [name, state] of [
    ["管理API", status.controlPlane === "ready" ? "稼働中" : "要確認"],
    ["定義の保存先", status.metadataStore === "ready" ? "D1 稼働中" : "要確認"],
    [
      "業務データ転送",
      status.dataPlane === "not_enabled" ? "未有効化" : "要確認",
    ],
  ]) {
    const card = node("div", undefined, "service");
    card.append(node("span", name), node("strong", state));
    services.append(card);
  }
  const history = byId("audit-list");
  history.replaceChildren();
  if (!audit.items.length) empty(history, "まだ操作履歴はありません。");
  const actions = {
    "source.created": "ソース登録",
    "source.updated": "ソース更新",
    "contract.draft_saved": "契約下書き保存",
  };
  for (const event of audit.items) {
    const card = node("article", undefined, "card");
    card.append(
      node(
        "strong",
        (actions[event.action] || "定義変更") + " · " + event.targetId,
      ),
      node(
        "p",
        "第" +
          event.revision +
          "版 / " +
          new Date(event.createdAt).toLocaleString("ja-JP"),
      ),
      node("small", "操作ID: " + event.requestId),
    );
    history.append(card);
  }
}
async function submit(form, task) {
  const button = form.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    await task();
    await refresh();
    notify(
      "下書きを保存しました。データ収集や実環境への適用は実行していません。",
    );
  } catch (error) {
    notify(error.message || "通信に失敗しました。", true);
  } finally {
    button.disabled = false;
  }
}
sourceForm.onsubmit = (event) => {
  event.preventDefault();
  submit(sourceForm, async () => {
    const body = Object.fromEntries(
      ["id", "displayName", "kind", "ownerTeam", "status"].map((key) => [
        key,
        field(sourceForm, key).value,
      ]),
    );
    if (sourceRevision !== undefined) body.expectedRevision = sourceRevision;
    await api(
      sourceRevision === undefined ? "/api/sources" : "/api/sources/" + body.id,
      sourceRevision === undefined ? "POST" : "PUT",
      body,
    );
    clearSource();
  });
};
contractForm.onsubmit = (event) => {
  event.preventDefault();
  submit(contractForm, async () => {
    const body = Object.fromEntries(
      ["id", "sourceId", "datasetId", "deletePolicy"].map((key) => [
        key,
        field(contractForm, key).value,
      ]),
    );
    body.freshnessMinutes = Number(
      field(contractForm, "freshnessMinutes").value,
    );
    body.expectedRevision = contractRevision;
    body.columns = [...byId("columns").children].map((row) =>
      Object.fromEntries(
        [...row.querySelectorAll("[data-field]")].map((input) => [
          input.dataset.field,
          input.type === "checkbox" ? input.checked : input.value,
        ]),
      ),
    );
    await api("/api/contracts", "POST", body);
    clearContract();
  });
};
for (const button of document.querySelectorAll("[data-tab]"))
  button.onclick = () => {
    for (const section of ["sources", "contracts", "operations"])
      byId(section).hidden = button.dataset.tab !== section;
    for (const tab of document.querySelectorAll("[data-tab]")) {
      if (tab === button) tab.setAttribute("aria-current", "page");
      else tab.removeAttribute("aria-current");
    }
  };
byId("new-source").onclick = clearSource;
byId("new-contract").onclick = clearContract;
byId("add-column").onclick = () => addColumn();
clearSource();
clearContract();
refresh().catch((error) => notify(error.message, true));
