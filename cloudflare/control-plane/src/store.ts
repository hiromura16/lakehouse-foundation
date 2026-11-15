import type { Contract, Source } from "./schema";

type AuditContext = { actor: string; requestId: string; now: string };

/** 名前付き列だけを返し、内部の監査識別子をソース一覧に混在させない。 */
export async function listSources(db: D1Database) {
  const result = await db
    .prepare(
      `SELECT id, display_name AS displayName, kind,
    owner_team AS ownerTeam, status, revision, updated_at AS updatedAt
    FROM sources ORDER BY id LIMIT 100`,
    )
    .all();
  return result.results;
}
export async function saveSource(
  db: D1Database,
  source: Source,
  audit: AuditContext,
  revision?: number,
) {
  const values = [
    source.displayName,
    source.kind,
    source.ownerTeam,
    source.status,
    audit.now,
    audit.actor,
    audit.requestId,
  ];
  const statement =
    revision === undefined
      ? db
          .prepare(
            `INSERT INTO sources
        (display_name,kind,owner_team,status,updated_at,actor_hash,request_id,id,revision)
        SELECT ?,?,?,?,?,?,?,?,1 WHERE (SELECT count(*) FROM sources) < 100
        ON CONFLICT(id) DO NOTHING RETURNING id`,
          )
          .bind(...values, source.id)
      : db
          .prepare(
            `UPDATE sources SET display_name=?,kind=?,owner_team=?,status=?,
        updated_at=?,actor_hash=?,request_id=?,revision=revision+1
        WHERE id=? AND revision=? RETURNING id`,
          )
          .bind(...values, source.id, revision);
  // D1のchangesには監査トリガーも含まれるため、返された本体行で判定する。
  const result = await statement.all();
  return result.results.length === 1;
}
export async function listContracts(db: D1Database) {
  const result = await db
    .prepare(
      `SELECT c.id,c.revision,c.definition_json,c.created_at
    FROM contracts c JOIN (SELECT id,MAX(revision) AS revision FROM contracts GROUP BY id) latest
    ON c.id=latest.id AND c.revision=latest.revision ORDER BY c.id LIMIT 100`,
    )
    .all<{
      id: string;
      revision: number;
      definition_json: string;
      created_at: string;
    }>();
  return result.results.map((row) => ({
    ...JSON.parse(row.definition_json),
    id: row.id,
    revision: row.revision,
    createdAt: row.created_at,
    status: "draft",
  }));
}
/** 条件付きINSERTで版を比較し、既存版を変更せず次の下書きを保存する。 */
export async function saveContract(
  db: D1Database,
  contract: Contract,
  audit: AuditContext,
) {
  const { expectedRevision, ...definition } = contract;
  const result = await db
    .prepare(
      `INSERT INTO contracts
    (id,revision,source_id,dataset_id,definition_json,created_at,actor_hash,request_id)
    SELECT ?,?,?,?,?,?,?,?
    WHERE COALESCE((SELECT MAX(revision) FROM contracts WHERE id=?),0)=?
    AND EXISTS(SELECT 1 FROM sources WHERE id=? AND status='draft')
    AND (EXISTS(SELECT 1 FROM contracts WHERE id=?) OR
         (SELECT COUNT(DISTINCT id) FROM contracts)<100)
    AND NOT EXISTS(SELECT 1 FROM contracts WHERE id=? AND (source_id<>? OR dataset_id<>?))
    ON CONFLICT(id,revision) DO NOTHING RETURNING id`,
    )
    .bind(
      contract.id,
      expectedRevision + 1,
      contract.sourceId,
      contract.datasetId,
      JSON.stringify(definition),
      audit.now,
      audit.actor,
      audit.requestId,
      contract.id,
      expectedRevision,
      contract.sourceId,
      contract.id,
      contract.id,
      contract.sourceId,
      contract.datasetId,
    )
    .all();
  return result.results.length === 1;
}
export async function listAudit(db: D1Database) {
  return (
    await db
      .prepare(
        `SELECT action,target_id AS targetId,revision,
    request_id AS requestId,created_at AS createdAt FROM audit_events
    ORDER BY sequence DESC LIMIT 50`,
      )
      .all()
  ).results;
}
