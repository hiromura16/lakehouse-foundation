import { z } from "zod";

// 接続文字列を受け取らず、論理識別子として使える文字だけを許可する。
const identifier = z.string().regex(/^[a-z][a-z0-9_-]{0,63}$/);
const displayName = z
  .string()
  .min(1)
  .max(80)
  .regex(/^[\p{L}\p{N} _-]+$/u);
export const sourceSchema = z.strictObject({
  id: identifier,
  displayName,
  kind: z.enum(["sqlserver", "postgresql", "file", "stream"]),
  ownerTeam: identifier,
  status: z.enum(["draft", "paused"]),
});
export const sourceUpdateSchema = sourceSchema.extend({
  expectedRevision: z.number().int().min(1).max(10000),
});
const columnSchema = z.strictObject({
  name: identifier,
  dataType: z.enum([
    "string",
    "int64",
    "decimal",
    "boolean",
    "date",
    "timestamp",
  ]),
  classification: z.enum(["public", "internal", "confidential"]),
  primaryKey: z.boolean(),
});
export const contractSchema = z
  .strictObject({
    id: identifier,
    sourceId: identifier,
    datasetId: identifier,
    columns: z.array(columnSchema).min(1).max(50),
    freshnessMinutes: z.number().int().min(1).max(43200),
    deletePolicy: z.enum(["retain-tombstone", "delete-in-silver"]),
    expectedRevision: z.number().int().min(0).max(99),
  })
  .superRefine((value, context) => {
    if (
      new Set(value.columns.map((column) => column.name)).size !==
      value.columns.length
    ) {
      context.addIssue({
        code: "custom",
        message: "列名は一意にしてください。",
      });
    }
    if (!value.columns.some((column) => column.primaryKey)) {
      context.addIssue({
        code: "custom",
        message: "主キーを1列以上指定してください。",
      });
    }
  });
export type Source = z.infer<typeof sourceSchema>;
export type Contract = z.infer<typeof contractSchema>;
