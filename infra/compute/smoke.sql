-- Only the dedicated compatibility namespace contains this synthetic fixture.
CREATE NAMESPACE IF NOT EXISTS lakehouse.compatibility;

-- Replacing one table keeps a repeated smoke run from appending duplicates.
CREATE OR REPLACE TABLE lakehouse.compatibility.spark_roundtrip
USING iceberg
TBLPROPERTIES ('format-version' = '2')
AS SELECT
    CAST(id AS BIGINT) AS id,
    CAST(label AS STRING) AS label,
    CAST(amount AS DECIMAL(18, 2)) AS amount
FROM VALUES
    (1, 'synthetic_alpha', 12.50),
    (2, 'synthetic_beta', 7.25),
    (3, '合成データ', 0.25)
AS fixture(id, label, amount);
