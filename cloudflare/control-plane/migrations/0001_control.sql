-- 業務データと物理接続情報を置かない、独立した管理用データベース。
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('sqlserver','postgresql','file','stream')),
  owner_team TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('draft','paused')),
  revision INTEGER NOT NULL CHECK (revision > 0),
  updated_at TEXT NOT NULL,
  actor_hash TEXT NOT NULL,
  request_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contracts (
  id TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK (revision BETWEEN 1 AND 100),
  source_id TEXT NOT NULL REFERENCES sources(id),
  dataset_id TEXT NOT NULL,
  definition_json TEXT NOT NULL CHECK (json_valid(definition_json)),
  created_at TEXT NOT NULL,
  actor_hash TEXT NOT NULL,
  request_id TEXT NOT NULL,
  PRIMARY KEY (id, revision)
);
CREATE INDEX IF NOT EXISTS contracts_source ON contracts(source_id);
CREATE TABLE IF NOT EXISTS audit_events (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  action TEXT NOT NULL,
  target_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  actor_hash TEXT NOT NULL,
  request_id TEXT NOT NULL,
  created_at TEXT NOT NULL
);
-- トリガーにより、変更と監査記録を同じトランザクションに含める。
CREATE TRIGGER IF NOT EXISTS sources_created AFTER INSERT ON sources BEGIN
  INSERT INTO audit_events(action,target_id,revision,actor_hash,request_id,created_at)
  VALUES ('source.created',NEW.id,NEW.revision,NEW.actor_hash,NEW.request_id,NEW.updated_at);
END;
CREATE TRIGGER IF NOT EXISTS sources_updated AFTER UPDATE ON sources BEGIN
  INSERT INTO audit_events(action,target_id,revision,actor_hash,request_id,created_at)
  VALUES ('source.updated',NEW.id,NEW.revision,NEW.actor_hash,NEW.request_id,NEW.updated_at);
END;
CREATE TRIGGER IF NOT EXISTS contracts_created AFTER INSERT ON contracts BEGIN
  INSERT INTO audit_events(action,target_id,revision,actor_hash,request_id,created_at)
  VALUES ('contract.draft_saved',NEW.id,NEW.revision,NEW.actor_hash,NEW.request_id,NEW.created_at);
END;
-- PoCの履歴は最新1000件。氏名・メール・本文・接続先を記録しない。
CREATE TRIGGER IF NOT EXISTS audit_bounded AFTER INSERT ON audit_events BEGIN
  DELETE FROM audit_events WHERE sequence <= NEW.sequence - 1000;
END;
